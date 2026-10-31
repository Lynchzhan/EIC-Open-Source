import os
from pathlib import Path

from fastapi.testclient import TestClient

TEST_DB = Path(__file__).with_name("test_eic.db")
os.environ.setdefault("EIC_DATABASE_URL", "sqlite+aiosqlite:///" + TEST_DB.as_posix())
os.environ.setdefault("EIC_BOOTSTRAP_ADMIN_PASSWORD", "TestOnly-Admin-Password-2026-Long")
os.environ.setdefault("EIC_JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")
os.environ.setdefault("EIC_SECRET_ENCRYPTION_KEY", "test-encryption-secret-with-at-least-thirty-two-characters")

from eic.main import MODULE_LABELS, app, stringify_unsafe_ints


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/Authorize/GetToken",
        json={"name": username, "passWord": password, "tenantId": 1000},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()}"}


def _module_pairs(node) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if isinstance(node, dict):
        if "moduleName" in node:
            pairs.append((str(node.get("module") or ""), str(node.get("moduleName") or "")))
        for value in node.values():
            pairs.extend(_module_pairs(value))
    elif isinstance(node, list):
        for item in node:
            pairs.extend(_module_pairs(item))
    return pairs


def test_stringify_unsafe_ints_preserves_bool_and_small_int() -> None:
    assert stringify_unsafe_ints(True) is True
    assert stringify_unsafe_ints(1) == 1
    huge = 2**53 + 7
    assert stringify_unsafe_ints(huge) == str(huge)
    payload = stringify_unsafe_ints({"id": huge, "ok": True, "items": [1, huge]})
    assert payload["id"] == str(huge)
    assert payload["ok"] is True
    assert payload["items"][1] == str(huge)


def test_register_user_profile_delete_404_module_labels_and_id_only_test_connection(monkeypatch) -> None:
    with TestClient(app) as client:
        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.post(
            "/api/Authorize/Register",
            json={"name": "newbie01", "passWord": "Newbie-Password-2026", "tenantId": 1000},
        ).status_code == 400
        code = client.post("/api/User/RegistrationCodes", headers=admin_headers)
        assert code.status_code == 200
        register = client.post(
            "/api/Authorize/Register",
            json={
                "name": "newbie01",
                "passWord": "Newbie-Password-2026",
                "tenantId": 1000,
                "nickName": "Newbie",
                "phone": "13800000001",
                "email": "newbie01@example.com",
                "registrationCode": code.json()["registrationCode"],
            },
        )
        assert register.status_code == 200
        assert register.json() is True
        pending_login = client.post(
            "/api/Authorize/GetToken",
            json={"name": "newbie01", "passWord": "Newbie-Password-2026", "tenantId": 1000},
        )
        assert pending_login.status_code == 400
        assert "待管理员审核" in pending_login.json()["detail"]["errMsg"]

        pending = client.get("/api/User/GetPendingApprovals", headers=admin_headers).json()
        newbie = next(item for item in pending if item["username"] == "newbie01")
        roles = client.get("/api/User/GetUserRoleKey", headers=admin_headers).json()
        standard = next(item for item in roles if item["value"] == "普通用户")
        assert client.post(
            "/api/User/ApproveRegistration",
            headers=admin_headers,
            json={"userId": newbie["id"], "roleId": standard["key"], "permissionIds": ["ai.chat.read", "ai.chat.write"]},
        ).json() is True
        newbie_headers = login(client, "newbie01", "Newbie-Password-2026")
        perms = client.get("/api/Permission/GetUserPermissions", headers=newbie_headers).json()
        assert "Menu/MyMenu/MyAiChat" in perms
        assert "ai.chat.write" in perms
        assert client.get("/api/User/GetAllUserCount", headers=newbie_headers).json()["total"] == 0
        assert client.post("/api/Role/AddEdit", headers=admin_headers, json={"name": "备注角色", "remarks": "正常使用的用户"}).json() is True
        remark_roles = client.post("/api/Role/GetPageData", headers=admin_headers, json={}).json()["data"]
        marked = next(item for item in remark_roles if item["name"] == "备注角色")
        assert marked["remarks"] == "正常使用的用户"
        assert client.post("/api/Department/AddEdit", headers=admin_headers, json={"name": "profile-dept"}).json() is True
        departments = client.get("/api/Department/GetALLList", headers=admin_headers).json()
        department = next(item for item in departments if item["name"] == "profile-dept")
        tree = client.get("/api/Department/GetDepartmentTree", headers=admin_headers).json()
        assert isinstance(tree, list) and tree
        for node in tree:
            if abs(int(node["id"])) > 2**53 - 1:
                assert isinstance(node["id"], str)
        assert client.post("/api/Position/AddEdit", headers=admin_headers, json={"name": "profile-position"}).json() is True
        positions = client.post("/api/Position/GetPageData", headers=admin_headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        position = next(item for item in positions if item["name"] == "profile-position")

        created = client.post(
            "/api/User/AddEdit",
            headers=admin_headers,
            json={
                "username": "profiled01",
                "password": "Profile-Password-2026",
                "phone": "13900001111",
                "email": "profiled01@example.com",
                "departmentId": department["id"],
                "positionId": position["id"],
                "dtoUserInfo": {"departmentId": department["id"]},
                "positions": [{"id": position["id"], "name": position["name"]}],
                "avatarUrl": "/uploads/demo-avatar.png",
            },
        )
        assert created.status_code == 200
        assert created.json() is True
        users = client.post("/api/User/GetPageData", headers=admin_headers, json={"searchKey": "profiled01"}).json()["data"]
        user = next(item for item in users if item["username"] == "profiled01")
        assert user["phone"] == "13900001111"
        assert user["email"] == "profiled01@example.com"
        assert user["departmentId"] == department["id"]
        assert user["dtoUserInfo"]["departmentName"] == "profile-dept"
        assert user["positions"][0]["id"] == position["id"]
        assert user["avatar"] == "/uploads/demo-avatar.png"
        assert user["avatarUrl"] == "/uploads/demo-avatar.png"

        missing_model = client.delete("/api/AIModels/Delete", params={"Id": 999999999}, headers=admin_headers)
        assert missing_model.status_code == 404
        assert "\u4e0d\u5b58\u5728" in missing_model.json()["detail"]["errMsg"]
        missing_app = client.delete("/api/AIApps/Delete", params={"Id": 999999999}, headers=admin_headers)
        assert missing_app.status_code == 404
        assert "\u4e0d\u5b58\u5728" in missing_app.json()["detail"]["errMsg"]

        roles = client.post("/api/Role/GetPageData", headers=admin_headers, json={}).json()["data"]
        area_tree = client.get(f"/api/Permission/GetAllAreaPermissions?roleId={roles[0]['id']}", headers=admin_headers).json()
        pairs = dict(_module_pairs(area_tree))
        assert pairs.get("user") == MODULE_LABELS["user"]
        assert pairs.get("model") == MODULE_LABELS["model"]
        assert pairs.get("department") == MODULE_LABELS["department"]
        assert MODULE_LABELS["user"] == "\u7528\u6237"
        assert all(name != key for key, name in pairs.items() if key in MODULE_LABELS)

        assert client.post(
            "/api/AIModels/AddEdit",
            headers=admin_headers,
            json={
                "endPoint": "http://127.0.0.1:9/v1",
                "modelName": "id-only-probe",
                "aiType": 1,
                "aiModelType": 1,
                "apiKey": "sk-test-not-real",
            },
        ).json() is True
        models = client.post("/api/AIModels/GetPageData", headers=admin_headers, json={"searchKey": "id-only-probe"}).json()["data"]
        model = next(item for item in models if item["modelName"] == "id-only-probe")

        called = {"value": False}

        async def fake_probe(probe_model, secret, *, prompt=None, timeout_seconds=None):
            assert secret == "sk-test-not-real"
            assert probe_model.endpoint.endswith("/v1")
            assert probe_model.model_name == "id-only-probe"
            assert prompt and timeout_seconds > 0
            called["value"] = True
            return {'message': 'synthetic-success'}

        monkeypatch.setattr("eic.main.probe_model_connection", fake_probe)
        endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/AIModels/TestConnection")
        cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__ or ()))
        monkeypatch.setattr(cells["probe_model_connection"], "cell_contents", fake_probe)
        probed = client.post("/api/AIModels/TestConnection", headers=admin_headers, json={"id": model["id"]})
        assert probed.status_code == 200
        data = probed.json()
        assert data['code'] == 200 and data['isSuccess'] is True
        assert data['data']['ok'] is True and data['data']['message'] == 'synthetic-success'
        assert data['data']['diagnostics']['configSource'] == 'saved'
        assert data['data']['diagnostics']['modelName'] == 'id-only-probe'
        assert data['data']['elapsedMs'] >= 0
        assert called["value"] is True

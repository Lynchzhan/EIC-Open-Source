import os
from pathlib import Path

from fastapi.testclient import TestClient

TEST_DB = Path(__file__).with_name("test_eic.db")
os.environ.setdefault("EIC_DATABASE_URL", "sqlite+aiosqlite:///" + TEST_DB.as_posix())
os.environ.setdefault("EIC_BOOTSTRAP_ADMIN_PASSWORD", "TestOnly-Admin-Password-2026-Long")
os.environ.setdefault("EIC_JWT_SECRET", "test-jwt-secret-with-at-least-thirty-two-characters")
os.environ.setdefault("EIC_SECRET_ENCRYPTION_KEY", "test-encryption-secret-with-at-least-thirty-two-characters")

from eic.main import app


def login(client: TestClient, username: str, password: str) -> dict[str, str]:
    response = client.post(
        "/api/Authorize/GetToken",
        json={"name": username, "passWord": password, "tenantId": 1000},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()}"}


def test_chats_tasks_agents_and_register_options_are_isolated() -> None:
    with TestClient(app) as client:
        options = client.get("/api/Authorize/RegisterOptions", params={"tenantId": 1000})
        assert options.status_code == 200
        assert "departments" in options.json()
        assert "positions" in options.json()

        admin_headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        assert client.post("/api/Department/AddEdit", headers=admin_headers, json={"name": "iso-dept"}).json() is True
        assert client.post("/api/Position/AddEdit", headers=admin_headers, json={"name": "iso-pos"}).json() is True
        departments = client.get("/api/Department/GetALLList", headers=admin_headers).json()
        positions = client.get("/api/Position/GetALLList", headers=admin_headers).json()
        department = next(item for item in departments if item["name"] == "iso-dept")
        position = next(item for item in positions if item["name"] == "iso-pos")

        public = client.get("/api/Authorize/RegisterOptions", params={"tenantId": 1000}).json()
        assert any(item["name"] == "iso-dept" for item in public["departments"])
        assert any(item["name"] == "iso-pos" for item in public["positions"])

        registration_input = {
            "name": "iso-user", "passWord": "Iso-User-Password-2026", "tenantId": 1000,
            "departmentId": department["id"], "positionId": position["id"],
        }
        assert client.post("/api/Authorize/Register", json=registration_input).status_code == 400
        code = client.post("/api/User/RegistrationCodes", headers=admin_headers)
        assert code.status_code == 200
        assert client.post(
            "/api/Authorize/Register",
            json={
                "name": "iso-user",
                "passWord": "Iso-User-Password-2026",
                "tenantId": 1000,
                "departmentId": department["id"],
                "positionId": position["id"],
                "registrationCode": code.json()["registrationCode"],
            },
        ).json() is True
        pending = client.get("/api/User/GetPendingApprovals", headers=admin_headers).json()
        newbie = next(item for item in pending if item["username"] == "iso-user")
        assert newbie["departmentId"] == department["id"]
        roles = client.get("/api/User/GetUserRoleKey", headers=admin_headers).json()
        standard = next(item for item in roles if item["value"] == "普通用户")
        assert client.post(
            "/api/User/ApproveRegistration",
            headers=admin_headers,
            json={"userId": newbie["id"], "roleId": standard["key"]},
        ).json() is True

        user_headers = login(client, "iso-user", "Iso-User-Password-2026")
        perms = client.get("/api/Permission/GetUserPermissions", headers=user_headers).json()
        assert "ai.chat.write" in perms
        assert "ai.resource.read" in perms
        assert "ai.resource.write" not in perms

        assert client.post("/api/AIChats/Add", headers=admin_headers, json={"name": "admin-only-chat"}).json() is True
        assert client.post("/api/AIChats/Add", headers=user_headers, json={"name": "user-only-chat"}).json() is True
        admin_chats = client.post("/api/AIChats/GetMyPageData", headers=admin_headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        user_chats = client.post("/api/AIChats/GetMyPageData", headers=user_headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        assert any(item["name"] == "admin-only-chat" for item in admin_chats)
        assert all(item["name"] != "user-only-chat" for item in admin_chats)
        assert any(item["name"] == "user-only-chat" for item in user_chats)
        assert all(item["name"] != "admin-only-chat" for item in user_chats)

        assert client.post("/api/AIKmss/AddEdit", headers=admin_headers, json={"name": "task-isolation-fixture"}).json() is True
        bases = client.post("/api/AIKmss/GetPageData", headers=admin_headers, json={"pageSize": 100}).json()["data"]
        base_id = next(item["id"] for item in bases if item["name"] == "task-isolation-fixture")
        task_input = {"handlerType": "knowledge-check", "knowledge_base_id": base_id}
        assert client.post("/api/AITasks/AddEdit", headers=admin_headers, json={"name": "invalid-task", "cron": "0 1 * * *"}).status_code == 400
        assert client.post("/api/AITasks/AddEdit", headers=admin_headers, json={"name": "admin-task", "cron": "0 1 * * *", "payload": task_input}).json() is True
        denied = client.post("/api/AITasks/AddEdit", headers=user_headers, json={"name": "user-task", "cron": "0 3 * * *", "payload": task_input})
        assert denied.status_code == 403
        assert denied.json()["detail"]["permission"] == "knowledge.read"
        assert client.post(
            "/api/Permission/EditAllAreaPermissions",
            headers=admin_headers,
            json={"roleId": standard["key"], "permissionIds": [*perms, "knowledge.read"]},
        ).json() is True
        assert "ai.resource.write" not in client.get("/api/Permission/GetUserPermissions", headers=user_headers).json()
        assert client.post("/api/AITasks/AddEdit", headers=user_headers, json={"name": "user-task", "cron": "0 3 * * *", "payload": task_input}).json() is True
        admin_tasks = {item["name"] for item in client.post("/api/AITasks/GetTaskList", headers=admin_headers).json()}
        user_tasks = {item["name"] for item in client.post("/api/AITasks/GetTaskList", headers=user_headers).json()}
        assert "admin-task" in admin_tasks
        assert "user-task" not in admin_tasks
        assert "user-task" in user_tasks
        assert "admin-task" not in user_tasks

        apps = client.get("/api/AIApps/GetMyALLList", headers=user_headers)
        assert apps.status_code == 200
        assert client.post("/api/AIApps/AddEdit", headers=user_headers, json={"name": "should-fail"}).status_code == 403

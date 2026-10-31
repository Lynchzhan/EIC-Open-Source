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


def test_login_writes_logs_and_message_aliases_and_dictionary_page() -> None:
    with TestClient(app) as client:
        bad = client.post(
            "/api/Authorize/GetToken",
            json={"name": "admin", "passWord": "wrong-password-value", "tenantId": 1000},
        )
        assert bad.status_code == 400
        assert "不正确" in bad.json()["detail"]["errMsg"]

        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        http_logs = client.post("/api/Log/GetHttpLogPageData", headers=headers, json={"pageNum": 1, "pageSize": 20})
        assert http_logs.status_code == 200
        assert http_logs.json()["total"] >= 1
        os_logs = client.post("/api/Log/GetOSLogPageData", headers=headers, json={"pageNum": 1, "pageSize": 20})
        assert os_logs.status_code == 200
        assert os_logs.json()["total"] >= 1

        assert client.post(
            "/api/Message/AddEdit",
            headers=headers,
            json={"title": "公告正文A", "messageContent": "公告内容A", "type": "announcement", "createUser": "admin"},
        ).json() is True
        assert client.post(
            "/api/Message/AddEdit",
            headers=headers,
            json={"title": "私信B", "content": "私信内容B", "type": "4"},
        ).json() is True

        announcements = client.post("/api/Message/GetAnnouncementPageData", headers=headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        privates = client.post("/api/Message/GetPrivateUserPageData", headers=headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        notice = next(item for item in announcements if item.get("title") == "公告正文A")
        private = next(item for item in privates if item.get("title") == "私信B")
        assert notice["messageContent"] == "公告内容A"
        assert notice["content"] == "公告内容A"
        assert notice["createUser"]
        assert all(item.get("title") != "私信B" for item in announcements)
        assert private["messageContent"] == "私信内容B"
        assert private["type"] in {"private-user", "4"}

        assert client.post(
            "/api/Dictionary/AddEdit",
            headers=headers,
            json={"type": "user_status", "key": "9", "value": "试用", "isSystem": False, "remarks": "test"},
        ).json() is True
        dictionaries = client.post("/api/Dictionary/GetPageData", headers=headers, json={"pageNum": 1, "pageSize": 50}).json()["data"]
        item = next(row for row in dictionaries if row.get("key") == "9" or row.get("value") == "试用")
        assert item["isSystem"] in {"否", False, "false", 0}

"""登录生命周期使用隔离数据库与真实 ASGI 路由，不发送模型请求。"""
from datetime import UTC, datetime, timedelta

import jwt
from fastapi.testclient import TestClient
import pytest

from test_rbac_and_organization import app, login, os
from eic.core.config import get_settings


def test_logout_removes_stream_cookie_and_cookie_only_access():
    with TestClient(app) as client:
        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        token = headers["Authorization"].split(" ", 1)[1]
        client.cookies.set("eic_stream_token", token, domain="testserver.local", path="/")
        # 不存在的合成 requestId 只验证认证，不生成对话或调用模型。
        stream = "/api/AIChatHistorys/Stream?requestId=authacceptanceabsent"
        assert client.get(stream).status_code == 404
        response = client.post("/api/Authorize/Logout", headers=headers)
        assert response.status_code == 200 and response.json() is True
        cookie = response.headers["set-cookie"].lower()
        assert "max-age=0" in cookie and "httponly" in cookie and "path=/" in cookie
        assert response.headers["cache-control"] == "no-store"
        assert "eic_stream_token" not in client.cookies
        assert client.get(stream).status_code == 401
        # 清理接口幂等，允许过期用户完成退出。
        assert client.post("/api/Authorize/Logout").status_code == 200


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/api/AIModels/GetALLList", None),
    ("GET", "/api/AIModels/Secret?Id=101", None),
    ("DELETE", "/api/AIModels/Delete?Id=101", None),
    ("POST", "/api/AIModels/AddEdit", {"modelName": "forbidden"}),
    ("GET", "/api/ExperienceAdmin/Settings", None),
])
def test_experience_cookie_never_authorizes_protected_product_api(method, path, payload):
    with TestClient(app) as client:
        # 即使客户端故意扩大 cookie 路径，正式认证入口也不接受体验凭据。
        client.cookies.set("eic_experience", "synthetic-experience-token", path="/")
        response = client.request(method, path, json=payload)
        assert response.status_code == 401
        assert "synthetic-experience-token" not in response.text


def test_expired_and_tampered_tokens_fail_bearer_and_stream_cookie():
    with TestClient(app) as client:
        headers = login(client, "admin", os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"])
        settings = get_settings()
        claims = jwt.decode(headers["Authorization"].split(" ", 1)[1], settings.jwt_secret,
                            algorithms=[settings.jwt_algorithm])
        claims["exp"] = datetime.now(UTC) - timedelta(seconds=10)
        expired = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        for invalid in (expired, "not-a-jwt"):
            assert client.get("/api/Permission/GetUserPermissions",
                              headers={"Authorization": "Bearer " + invalid}).status_code == 401
            client.cookies.set("eic_stream_token", invalid, domain="testserver.local", path="/")
            assert client.get("/api/AIChatHistorys/Stream?requestId=authacceptanceabsent").status_code == 401
        assert client.post("/api/Authorize/Logout").status_code == 200

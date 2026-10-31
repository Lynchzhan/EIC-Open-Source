import json
from io import BytesIO
import os
import time
from pathlib import Path
import zipfile

from fastapi.testclient import TestClient

TEST_DB = Path(__file__).with_name("test_skill_filter.db")
os.environ["EIC_DATABASE_URL"] = "sqlite+aiosqlite:///" + TEST_DB.as_posix()
os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"] = "TestOnly-Admin-Password-2026-Long"
os.environ["EIC_JWT_SECRET"] = "test-jwt-secret-with-at-least-thirty-two-characters"
os.environ["EIC_SECRET_ENCRYPTION_KEY"] = "test-encryption-secret-with-at-least-thirty-two-characters"

from eic.main import app, skill_tool_type_value


def login(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/Authorize/GetToken",
        json={"name": "admin", "passWord": os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()}"}


def test_skill_tool_type_value_normalizes_filter():
    assert skill_tool_type_value(None) is None
    assert skill_tool_type_value(0) is None
    assert skill_tool_type_value("2") == 2
    assert skill_tool_type_value(3) == 3
    assert skill_tool_type_value(9) is None


def test_skill_page_filters_tool_skill_mcp(monkeypatch, tmp_path):
    # 合成库由 fixture 显式迁移；启动前不再删除已经验证的 schema。
    stamp = str(int(time.time() * 1000))
    from eic import main
    monkeypatch.setattr(main, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(main.settings, "upload_root", str(tmp_path))
    with TestClient(app) as client:
        headers = login(client)
        body = BytesIO()
        with zipfile.ZipFile(body, "w") as archive:
            archive.writestr("SKILL.md", "---\nname: filter-fixture\n---\nMetadata only")
        upload = client.post("/api/File/UploadFile", headers=headers,
                             files={"file": ("filter.zip", body.getvalue(), "application/zip")})
        assert upload.status_code == 200
        file_id = upload.json()["id"]
        created = []
        for name, method, skill_type, extra in [
            (f"filter-tool-{stamp}", "filter_tool", 1, {}),
            (f"filter-skill-{stamp}", "filter_skill", 2, {"fileId": file_id, "shareSkillFile": True}),
            (f"filter-mcp-{stamp}", "filter_mcp", 3, {"mcpUrl": "https://mcp.example.local/demo", "mcpType": "https"}),
        ]:
            payload = {"name": name, "classMethod": method, "skillToolType": skill_type, "activeStatus": 1, "description": name}
            payload.update(extra)
            response = client.post("/api/AISkillToolManagement/AddEdit", headers=headers, json=payload)
            assert response.status_code == 200, response.text
            assert response.json() is True
            created.append((name, skill_type))

        def names(parameter):
            body = {"pageNum": 1, "pageSize": 200, "parameter": parameter}
            response = client.post("/api/AISkillToolManagement/GetPageData", headers=headers, json=body)
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            return {item["name"] for item in data}, {item["name"]: item.get("skillToolType") for item in data}

        all_names, _ = names(0)
        assert {item[0] for item in created} <= all_names

        skill_names, skill_types = names(2)
        assert f"filter-skill-{stamp}" in skill_names
        assert f"filter-tool-{stamp}" not in skill_names
        assert f"filter-mcp-{stamp}" not in skill_names
        assert all(int(skill_types[name]) == 2 for name in skill_names)

        mcp_names, mcp_types = names(3)
        assert f"filter-mcp-{stamp}" in mcp_names
        assert f"filter-tool-{stamp}" not in mcp_names
        assert f"filter-skill-{stamp}" not in mcp_names
        assert all(int(mcp_types[name]) == 3 for name in mcp_names)

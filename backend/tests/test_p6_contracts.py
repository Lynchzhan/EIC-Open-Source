"""P6 合成账户、受控文件与 Mock provider 正反契约，不使用现网业务数据。"""
import asyncio
from io import BytesIO
import itertools
import struct
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from eic import main
from eic.core.database import SessionLocal, engine
from eic.core.security import hash_password, verify_password
from auth_fixture import signed_headers
from eic.models import AIModel, ChatEvent, ChatMessage, Resource, Tenant, User

SERIAL = itertools.count(960000)
OLD = "P6-Only-Old-Password-2026"
NEW = "P6-Only-New-Password-2026"


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    tenant = next(SERIAL)
    user, other = tenant + 100000, tenant + 200000
    password_hash = hash_password(OLD)
    async def seed():
        async with SessionLocal() as session:
            session.add(Tenant(id=tenant, code=tenant, name="P6 fixture"))
            await session.flush()
            for identity in (user, other):
                session.add(User(id=identity, tenant_id=tenant, username=str(identity),
                                 password_hash=password_hash, is_super_admin=True))
            await session.commit()
        await engine.dispose()
    asyncio.run(seed())
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(main, "UPLOAD_ROOT", root)
    monkeypatch.setattr(main.settings, "upload_root", str(root))
    headers = lambda uid: signed_headers(uid, tenant, str(uid), password_hash)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, headers=headers(user), other_headers=headers(other),
                              user=user, tenant=tenant, root=root)


def changed_password(f):
    async def read():
        async with SessionLocal() as session:
            return (await session.get(User, f.user)).password_hash
    return asyncio.run(read())


@pytest.mark.parametrize("old_key,new_key", [
    ("OldPwd", "NewPwd"), ("oldPassword", "newPassword"),
    ("oldPassWord", "passWord"), ("oldPassword", "password"),
])
def test_password_aliases_and_next_login(fixture, old_key, new_key):
    f = fixture
    result = f.client.post("/api/User/ChangePasswordTokenUser", headers=f.headers,
                           json={old_key: OLD, new_key: NEW})
    assert result.status_code == 200 and result.json() is True
    assert verify_password(NEW, changed_password(f)) and not verify_password(OLD, changed_password(f))
    for password, status in ((OLD, 400), (NEW, 200)):
        login = f.client.post("/api/Authorize/GetToken",
                              json={"name": str(f.user), "passWord": password, "tenantId": f.tenant})
        assert login.status_code == status


@pytest.mark.parametrize("payload", [
    {"oldPassword": "wrong", "newPassword": NEW}, {"newPassword": NEW},
    {"oldPassword": OLD}, {"oldPassword": OLD, "newPassword": True},
    {"oldPassword": OLD, "newPassword": "short"},
    {"OldPwd": OLD, "oldPassword": "different", "NewPwd": NEW},
    {"oldPassword": OLD, "newPassword": NEW, "NewPwd": "different-password"},
    {"oldPassword": [OLD], "newPassword": NEW},
])
def test_invalid_password_never_changes_hash(fixture, payload, caplog):
    before = changed_password(fixture)
    result = fixture.client.post("/api/User/ChangePasswordTokenUser", headers=fixture.headers, json=payload)
    assert result.status_code == 400
    assert changed_password(fixture) == before
    assert OLD not in caplog.text and NEW not in caplog.text


def upload(f, filename="evidence.txt", body=b"P6 attachment evidence", content_type="text/plain"):
    response = f.client.post("/api/File/UploadFile", headers=f.headers,
                             files={"file": (filename, body, content_type)})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def share(f, file_id):
    response = f.client.post("/api/File/SetSharing", headers=f.headers,
                             json={"id": file_id, "accessMode": "tenant"})
    assert response.status_code == 200 and response.json()["accessMode"] == "tenant"


def zip_body(names=("SKILL.md",)):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.writestr(name, "---\nname: fixture\n---\nMetadata only")
    return output.getvalue()


def test_chat_capabilities_report_current_features_and_attachment_switch(fixture, monkeypatch):
    monkeypatch.setattr(main.settings, 'chat_attachments_enabled', False)
    result = fixture.client.get("/api/AIChatHistorys/Capabilities", headers=fixture.headers)
    assert result.status_code == 200
    data = result.json()
    assert data["onlineSearch"] is True and data["skillExecution"] is True and data["mcpExecution"] is True
    assert data["attachments"] is False and data["attachmentFormats"] == []


def test_file_bearer_anonymous_and_private_owner(fixture):
    f = fixture
    file_id = upload(f)
    url = "/api/File/GetFile?fileid=" + str(file_id)
    assert f.client.get(url).status_code == 401
    assert f.client.get(url, headers=f.other_headers).status_code == 404
    assert f.client.get(url, headers=f.headers).content == b"P6 attachment evidence"
    token = f.headers["Authorization"].split(" ", 1)[1]
    f.client.cookies.set("eic_stream_token", token)
    # stream cookie 仅用于 SSE，不扩大私有文件 API 的 cookie 认证范围。
    assert f.client.get(url).status_code == 401
    f.client.cookies.clear()
    assert f.client.get(url).status_code == 401


@pytest.mark.parametrize("names", [("../SKILL.md",), ("SKILL.md", "../evil.py"), ("other.txt",)])
def test_invalid_skill_zip_has_no_partial_resource(fixture, names):
    f = fixture
    file_id = upload(f, "skill.zip", zip_body(names), "application/zip")
    share(f, file_id)
    response = f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers,
                             json={"name": "invalid-p6-skill", "skillToolType": 2, "fileId": file_id})
    assert response.status_code == 400
    async def count():
        async with SessionLocal() as session:
            return list(await session.scalars(select(Resource.id).where(
                Resource.tenant_id == f.tenant, Resource.resource_type == "skills")))
    assert asyncio.run(count()) == []


def test_skill_file_roundtrip_and_private_rejection(fixture):
    f = fixture
    file_id = upload(f, "skill.zip", zip_body(), "application/zip")
    payload = {"name": "p6-skill", "skillToolType": 2, "fileId": file_id}
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers, json=payload).status_code == 400
    share(f, file_id)
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers, json=payload).json() is True
    page = f.client.post("/api/AISkillToolManagement/GetPageData", headers=f.headers,
                         json={"pageSize": 100}).json()
    item = next(x for x in page["data"] if x["name"] == "p6-skill")
    assert item["fileId"] == file_id and item["skillFile"]["id"] == file_id
    assert item["executionMode"] == "metadata" and item["executionEnabled"] is False


def test_attachment_body_provider_and_history_roundtrip(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr(main.settings, "chat_attachment_max_bytes", 4096)
    monkeypatch.setattr(main.settings, "chat_attachment_max_chars", 4096)
    monkeypatch.setattr(main.settings, "chat_attachment_max_files", 3)
    monkeypatch.setattr(main.settings, "chat_attachments_enabled", True)
    file_id = upload(f, "evidence.txt", "受控附件正文 P6_SENTINEL".encode())
    seen = []
    async def seed_model():
        async with SessionLocal() as session:
            session.add(AIModel(id=f.tenant+300000, tenant_id=f.tenant,
                                model_name="fixture", provider="openai_compatible", model_type="chat",
                                endpoint="http://fixture.invalid", enabled=True))
            await session.commit()
    asyncio.run(seed_model())
    async def provider(model, question, **kwargs):
        seen.append(kwargs["messages"])
        yield "附件已作为本轮用户材料提供"
    monkeypatch.setattr(main, "stream_chat_completion", provider)
    added = f.client.post("/api/AIChatHistorys/Add", headers=f.headers,
                          json={"id": "p6-" + str(f.tenant), "content": "概括附件", "fileIds": [file_id]})
    assert added.status_code == 200, added.text
    result = f.client.get(added.json()["streamUrl"], headers=f.headers)
    assert result.status_code == 200 and '"status": "completed"' in result.text
    assert len(seen) == 1
    assert "P6_SENTINEL" in seen[0][-1]["content"] and seen[0][-1]["role"] == "user"
    assert "aIToolsContentMsg" not in result.text and "本轮可用技能" not in result.text
    history = f.client.post("/api/AIChatHistorys/GetPageData", headers=f.headers,
                           json={"aIChatsId": added.json()["aIChatsId"], "pageSize": 100}).json()["data"]
    message = next(x for x in history if x["isSend"])
    assert message["fileIds"] == [file_id]
    assert message["attachments"][0]["name"] == "evidence.txt"
    assert "path" not in str(message["attachments"])
    assert f.client.post("/api/AIChatHistorys/Add", headers=f.other_headers,
                         json={"content": "denied", "fileIds": [file_id]}).status_code == 404


@pytest.mark.parametrize("filename,body", [
    ("image.png", b"not-vision"), ("bad.txt", b"\xff\xfe"),
    ("huge.txt", b"x" * 200), ("empty.txt", b""),
])
def test_attachment_rejection_precedes_acceptance(fixture, monkeypatch, filename, body):
    monkeypatch.setattr(main.settings, "chat_attachments_enabled", True)
    monkeypatch.setattr(main.settings, "chat_attachment_max_files", 3)
    monkeypatch.setattr(main.settings, "chat_attachment_max_bytes", 100)
    monkeypatch.setattr(main.settings, "chat_attachment_max_chars", 100)
    file_id = upload(fixture, filename, body)
    response = fixture.client.post("/api/AIChatHistorys/Add", headers=fixture.headers,
                                    json={"content": "do not pretend to read", "fileIds": [file_id]})
    assert response.status_code == 400


def test_online_search_rejected_not_silently_ignored(fixture, monkeypatch):
    calls = []
    async def seed():
        async with SessionLocal() as session:
            session.add(AIModel(id=fixture.tenant+300000, tenant_id=fixture.tenant,
                model_name='disabled-search', provider='openai_compatible', model_type='chat',
                endpoint='https://provider.fixture/v1', enabled=True, search_mode='disabled'))
            await session.commit()
    asyncio.run(seed())
    async def provider(*args, **kwargs):
        calls.append(True)
        yield 'must-not-run'
    monkeypatch.setattr(main, 'stream_chat_completion', provider)
    result = fixture.client.post("/api/AIChatHistorys/Add", headers=fixture.headers,
                                 json={"content": "search", "isOnlineSearch": True})
    assert result.status_code == 200
    stream = fixture.client.get(result.json()['streamUrl'], headers=fixture.headers)
    assert '"status": "failed"' in stream.text
    assert 'search' in stream.text
    assert calls == []


def test_skill_sharing_and_binding_are_atomic(fixture):
    f = fixture
    bad_id = upload(f, "invalid.zip", zip_body(("other.txt",)), "application/zip")
    body = {"name": "atomic", "skillToolType": 2, "fileId": bad_id, "shareSkillFile": True}
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers, json=body).status_code == 400
    metadata = f.client.get("/api/File/GetById", headers=f.headers, params={"Id": bad_id}).json()
    assert metadata["accessMode"] == "private"
    assert f.client.get("/api/File/GetFile", headers=f.other_headers, params={"fileid": bad_id}).status_code == 404
    file_id = upload(f, "valid.zip", zip_body(), "application/zip")
    body["fileId"] = file_id
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers, json=body).status_code == 200
    assert f.client.get("/api/File/GetById", headers=f.headers, params={"Id": file_id}).json()["accessMode"] == "tenant"
    assert f.client.get("/api/File/GetFile", headers=f.other_headers, params={"fileid": file_id}).status_code == 200


def test_file_skill_without_zip_reference_is_rejected(fixture):
    assert fixture.client.post("/api/AISkillToolManagement/AddEdit", headers=fixture.headers,
                               json={"name": "missing", "skillToolType": 2}).status_code == 400


def test_partial_file_skill_edit_cannot_bypass_zip_validation(fixture):
    f = fixture
    file_id = upload(f, "original.zip", zip_body(), "application/zip")
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers,
                         json={"name": "partial-edit", "skillToolType": 2,
                               "fileId": file_id, "shareSkillFile": True}).status_code == 200
    page = f.client.post("/api/AISkillToolManagement/GetPageData", headers=f.headers,
                         json={"pageSize": 100}).json()["data"]
    item = next(x for x in page if x["name"] == "partial-edit")
    invalid = upload(f, "not-zip.txt", b"not a zip")
    # 省略类型字段时仍按持久化的 Skill 类型检验，不允许换成任意文本。
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers,
                         json={"id": item["id"], "name": "partial-edit",
                               "fileId": invalid, "shareSkillFile": True}).status_code == 400
    current = f.client.get("/api/AISkillToolManagement/GetById",
                           headers=f.headers, params={"Id": item["id"]}).json()
    assert current["fileId"] == file_id and current["skillToolType"] == 2
    assert f.client.get("/api/File/GetById", headers=f.headers,
                         params={"Id": invalid}).json()["accessMode"] == "private"
    assert f.client.post("/api/AISkillToolManagement/AddEdit", headers=f.headers,
                         json={"id": item["id"], "name": "partial-edit-renamed"}).status_code == 200
    current = f.client.get("/api/AISkillToolManagement/GetById",
                           headers=f.headers, params={"Id": item["id"]}).json()
    assert current["fileId"] == file_id and current["skillToolType"] == 2


def test_skill_zip_corrupt_nonmanifest_entry_rejected_atomically(fixture):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("SKILL.md", "valid metadata")
        archive.writestr("notes.txt", "CRC-PAYLOAD")
    corrupt = output.getvalue().replace(b"CRC-PAYLOAD", b"BAD-PAYLOAD")
    file_id = upload(fixture, "corrupt.zip", corrupt, "application/zip")
    result = fixture.client.post("/api/AISkillToolManagement/AddEdit", headers=fixture.headers,
                                 json={"name": "bad-crc", "skillToolType": 2,
                                       "fileId": file_id, "shareSkillFile": True})
    assert result.status_code == 400
    assert fixture.client.get("/api/File/GetById", headers=fixture.headers,
                               params={"Id": file_id}).json()["accessMode"] == "private"


def test_skill_zip_corrupt_deflated_entry_rejected_atomically(fixture):
    output = BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("SKILL.md", "valid metadata")
        archive.writestr("notes.txt", "compressed notes for validation")
    corrupt = bytearray(output.getvalue())
    with zipfile.ZipFile(BytesIO(corrupt)) as archive:
        entry = archive.getinfo("notes.txt")
        name_size, extra_size = struct.unpack_from("<HH", corrupt, entry.header_offset + 26)
        offset = entry.header_offset + 30 + name_size + extra_size
    # 非法 DEFLATE block type；应作为格式错误拒绝，而不是泄漏成服务器 500。
    corrupt[offset] = 0x07
    file_id = upload(fixture, "bad-deflate.zip", bytes(corrupt), "application/zip")
    result = fixture.client.post("/api/AISkillToolManagement/AddEdit", headers=fixture.headers,
                                 json={"name": "bad-deflate", "skillToolType": 2,
                                       "fileId": file_id, "shareSkillFile": True})
    assert result.status_code == 400
    metadata = fixture.client.get("/api/File/GetById", headers=fixture.headers,
                                  params={"Id": file_id}).json()
    assert metadata["accessMode"] == "private"
    page = fixture.client.post("/api/AISkillToolManagement/GetPageData", headers=fixture.headers,
                               json={"pageSize": 100}).json()["data"]
    assert not any(item["name"] == "bad-deflate" for item in page)


@pytest.mark.parametrize("payload", [
    {"fileIds": ["not-id"]}, {"fileIds": [True]}, {"fileIds": "123"},
    {"file": "https://foreign.invalid/private"}, {"attachments": [{"url": "file:///secret"}]},
    {"fileIds": [], "contentFileUrls": "https://foreign.invalid/private"},
])
def test_invalid_attachment_references_not_accepted(fixture, payload):
    response = fixture.client.post("/api/AIChatHistorys/Add", headers=fixture.headers,
                                   json={"content": "invalid", **payload})
    assert response.status_code == 400


def test_attachment_total_budget_and_disabled_configuration(fixture, monkeypatch):
    f = fixture
    ids = [upload(f, str(index) + ".txt", b"x" * 60) for index in range(2)]
    monkeypatch.setattr(main.settings, "chat_attachments_enabled", True)
    for field, value in (("chat_attachment_max_bytes", 100), ("chat_attachment_max_chars", 100),
                         ("chat_attachment_max_files", 3)):
        monkeypatch.setattr(main.settings, field, value)
    for supplied in (ids, ids + ids):
        assert f.client.post("/api/AIChatHistorys/Add", headers=f.headers,
                             json={"content": "budget", "fileIds": supplied}).status_code == 400
    monkeypatch.setattr(main.settings, "chat_attachment_max_bytes", None)
    capabilities = f.client.get("/api/AIChatHistorys/Capabilities", headers=f.headers).json()
    assert capabilities["attachments"] is False
    assert f.client.post("/api/AIChatHistorys/Add", headers=f.headers,
                         json={"content": "disabled", "fileIds": [ids[0]]}).status_code == 400


def test_resume_replay_and_native_header_cursor(fixture):
    f = fixture
    result = f.client.post("/api/AIChatHistorys/Add", headers=f.headers,
                           json={"id": "p6-resume-" + str(f.tenant), "content": "replay"}).json()
    rid = result["id"]
    initial = f.client.get(result["streamUrl"], headers=f.headers)
    assert initial.status_code == 200 and '"status": "failed"' in initial.text
    assert '"reason": "model_not_configured"' in initial.text
    resumed = f.client.post("/api/AIChatHistorys/Resume", params={"requestId": rid}, headers=f.headers)
    assert resumed.status_code == 200 and resumed.json()["id"] == rid
    assert f.client.post("/api/AIChatHistorys/Resume", params={"requestId": rid},
                         headers=f.other_headers).status_code == 404
    async def sequences():
        async with SessionLocal() as session:
            return list(await session.scalars(select(ChatEvent.sequence).where(ChatEvent.request_id == rid).order_by(ChatEvent.sequence)))
    events = asyncio.run(sequences())
    assert len(events) >= 2
    # URL 固定在首次游标，原生 EventSource Header 更新为较新游标。
    stream = f.client.get(result["streamUrl"] + "&afterEventId=" + rid + "-" + str(events[0]),
                          headers={**f.headers, "Last-Event-ID": rid + "-" + str(events[1])})
    assert stream.status_code == 200
    assert "id: " + rid + "-" + str(events[0]) + "\n" not in stream.text
    assert "id: " + rid + "-" + str(events[1]) + "\n" not in stream.text
    assert f.client.get(result["streamUrl"] + "&afterEventId=wrong-1", headers=f.headers).status_code == 400
    retried = f.client.post("/api/AIChatHistorys/Add", headers=f.headers,
                            json={"id": rid, "content": "replay"})
    assert retried.status_code == 200 and retried.json()["id"] == rid
    assert asyncio.run(sequences()) == events

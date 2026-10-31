"""体验能力的隔离回归：仅临时 SQLite、临时文件和内存 ASGI 请求。"""
import asyncio
from contextlib import asynccontextmanager
import copy
import importlib
import json
import socket
import sqlite3
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError


MENUS = (
    "chat", "knowledge", "agents", "models", "tasks", "memory", "observability",
    "prompts", "skills", "departments", "positions", "users", "roles", "permissions",
    "announcements", "settings", "tenants", "experience", "logs", "audit", "codegen",
    "messages", "profile",
)
CAPABILITIES = ("allow_chat_attachments", "allow_web_search")


@pytest.fixture
async def file_sandbox(tmp_path, monkeypatch):
    from eic.core import config

    # model_construct 不读取 .env 或继承真实部署凭据。
    settings = config.Settings.model_construct(
        database_url="sqlite+aiosqlite:///:memory:",
        experience_root=str(tmp_path / "control"),
        experience_content_root=str(tmp_path / "content"),
        chat_attachment_max_bytes=4096, chat_attachment_max_chars=4096,
        chat_attachment_max_files=2,
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    def forbidden(*args, **kwargs):
        pytest.fail("隔离测试禁止网络、真实业务库或未替换的解析子进程")

    # 先由 pytest-asyncio 建好 Windows 自唤醒 socket pair，再拦截业务网络。
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    files = importlib.import_module("eic.experience_files")
    monkeypatch.setattr(files, "get_settings", lambda: settings)
    monkeypatch.setattr(files.asyncio, "create_subprocess_exec", forbidden)
    return SimpleNamespace(files=files, settings=settings, root=tmp_path,
                           monkeypatch=monkeypatch, forbidden=forbidden)


@pytest.fixture
async def sandbox(file_sandbox):
    settings = file_sandbox.settings
    monkeypatch = file_sandbox.monkeypatch
    forbidden = file_sandbox.forbidden
    exp = importlib.import_module("eic.experience")
    monkeypatch.setattr(exp, "get_settings", lambda: settings)
    monkeypatch.setattr(exp, "SessionLocal", forbidden)
    monkeypatch.setattr(exp, "RUNNING", {})
    from eic.core import database
    monkeypatch.setattr(database, "SessionLocal", forbidden)
    principal = SimpleNamespace(is_super_admin=True, tenant_id=1)

    async def authenticated():
        return principal

    app = FastAPI()
    exp.bind(app, authenticated, forbidden)
    with exp.database() as db:
        exp.put(db, "settings", "settings", "global", {**copy.deepcopy(exp.DEFAULT), "enabled": True})
    return SimpleNamespace(exp=exp, files=file_sandbox.files, settings=settings, app=app,
                           principal=principal, root=file_sandbox.root, monkeypatch=monkeypatch)


@pytest.fixture
async def client(sandbox):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sandbox.app), base_url="http://experience.test",
        headers={"x-eic-experience": "1"},
    ) as value:
        yield value


def seed(sandbox, *, mode="real", token="fixture-token", **changes):
    exp = sandbox.exp
    now = exp.time.time()
    key = "session:" + exp.digest(token)
    grant_id = "grant:" + exp.digest(token)[:32]
    grant = dict(tenant="1", kind="link", created=now - 10, started=now - 5,
                 claimEnd=now + 3600, hardEnd=now + 7200, expires=now + 1800,
                 calls=10, tokens=100000, minutes=30, usedCalls=0, usedTokens=0,
                 busyUntil=0, pages=["chat"], modelIds=["101"], agentIds=[],
                 knowledgeIds=[], knowledgeModels={}, sessionKey=key,
                 linkHash=exp.digest("synthetic-ticket"), linkEncrypted="synthetic-cipher",
                 history=[], revision=1)
    grant.update(changes)
    state = dict(mode=mode, pages=grant["pages"], expires=grant["expires"])
    if mode == "real":
        state["grant"] = grant_id
    with exp.database() as db:
        if mode == "real":
            exp.put(db, grant_id, "grant", grant["tenant"], grant)
        exp.put(db, key, "session", "ephemeral", state)
    return SimpleNamespace(token=token, key=key, grant_id=grant_id, grant=grant)


def authenticate(client, record):
    client.cookies.set("eic_experience", record.token, path="/api/experience")


def add_content(sandbox, record):
    exp = sandbox.exp
    attachment_id = "attachment:" + exp.digest(record.token)[:32]
    path = sandbox.files.attachment_path(record.key, attachment_id, ".txt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private fixture")
    with exp.database() as db:
        db.execute("INSERT INTO ephemeral.content VALUES (?,?,?)",
                   (record.key, record.grant["expires"], json.dumps([{"role": "user", "content": "private"}])))
        db.execute("INSERT INTO ephemeral.attachments (id,session_id,expires,name,content) VALUES (?,?,?,?,?)",
                   (attachment_id, record.key, record.grant["expires"], "fixture.txt", "private fixture"))
    return attachment_id, path


def assert_cleaned(sandbox, record, path, reason):
    exp = sandbox.exp
    with exp.database() as db:
        assert exp.load(db, record.key) is None
        assert db.execute("SELECT count(*) FROM ephemeral.content WHERE id=?", (record.key,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM ephemeral.attachments WHERE session_id=?", (record.key,)).fetchone()[0] == 0
        grant = exp.load(db, record.grant_id)
        assert grant["closedReason"] == reason
        assert grant["revoked"] is True
        assert not set(("sessionKey", "linkHash", "linkEncrypted", "passwordHash", "modelIds", "agentIds", "knowledgeIds", "knowledgeModels")) & grant.keys()
        assert grant["usedCalls"] == record.grant["usedCalls"]
        assert exp.load(db, "usage") == {"calls": 7, "tokens": 8000}
    assert not path.parent.exists()


@pytest.mark.parametrize("model_name", ["GrantInput", "GrantUpdate"])
def test_capabilities_default_false_and_require_strict_boolean(sandbox, model_name):
    model = getattr(sandbox.exp, model_name)
    data = {"modelIds": ["101"]}
    if model_name == "GrantUpdate":
        data.update(revision=1, minutes=30, claimEnd=100, hardEnd=200,
                    calls=10, tokens=1024, pages=["chat"])
    assert all(model(**data).model_dump()[name] is False for name in CAPABILITIES)
    for name in CAPABILITIES:
        for invalid in ("true", 1, None):
            with pytest.raises(ValidationError):
                model(**{**data, name: invalid})


@pytest.mark.parametrize("mode", ["public", "real"])
async def test_default_capabilities_rejected_by_http_before_side_effects(sandbox, client, mode):
    record = seed(sandbox, mode=mode)
    authenticate(client, record)
    response = await client.get("/api/experience/session")
    assert response.status_code == 200
    assert all(response.json()[name] is False for name in (*CAPABILITIES, "web_search_available"))
    for method, url, kwargs in (
        ("POST", "/attachments", {"content": b"fixture", "headers": {"x-experience-filename": "fixture.txt"}}),
        ("GET", "/attachments", {}),
        ("DELETE", "/attachments/attachment:" + "a" * 32, {}),
        ("POST", "/chat", {"json": {"message": "fixture", "web_search": True}}),
        ("POST", "/chat", {"json": {"message": "fixture", "attachment_ids": ["attachment:" + "a" * 32]}}),
    ):
        denied = await client.request(method, "/api/experience" + url, **kwargs)
        assert denied.status_code == 403, denied.text
    with sandbox.exp.database() as db:
        assert sandbox.exp.load(db, "usage") is None
        assert db.execute("SELECT count(*) FROM ephemeral.attachments").fetchone()[0] == 0
    assert not sandbox.files.sessions_root().exists()


@pytest.mark.parametrize("mode", ["public", "real"])
@pytest.mark.parametrize("page", MENUS)
async def test_every_menu_virtual_preview_never_accesses_business_database(sandbox, client, mode, page):
    assert set(MENUS) == set(sandbox.exp.PREVIEW_PAGES)
    record = seed(sandbox, mode=mode)
    # 真实会话的未授权页面也只能返回虚拟数据。
    if mode == "real":
        with sandbox.exp.database() as db:
            cfg = sandbox.exp.config(db)
            cfg["realPages"] = []
            sandbox.exp.put(db, "settings", "settings", "global", cfg)
    authenticate(client, record)
    response = await client.get("/api/experience/catalog", params={"page": page})
    assert response.status_code == 200, response.text
    assert response.json()["preview"] is True
    assert "101" not in response.text
    assert response.headers["cache-control"] == "no-store"
    with sandbox.exp.database() as db:
        assert sandbox.exp.load(db, "usage") is None


async def test_unknown_preview_and_missing_session_are_rejected(sandbox, client):
    assert (await client.get("/api/experience/catalog")).status_code == 401
    authenticate(client, seed(sandbox, mode="public"))
    assert (await client.get("/api/experience/catalog?page=not-a-menu")).status_code == 404


@pytest.mark.parametrize("extra", [
    {"modelId": "999"}, {"modelId": "101", "agentId": "999"},
    {"modelId": "101", "knowledgeIds": ["999"]},
])
async def test_chat_rejects_unauthorized_resources_before_database_or_provider(sandbox, client, extra):
    authenticate(client, seed(sandbox))
    response = await client.post("/api/experience/chat", json={"message": "fixture", **extra})
    assert response.status_code == 403


@pytest.mark.parametrize("capability", CAPABILITIES)
@pytest.mark.parametrize("block", ["public", "no-chat", "grant-calls", "grant-tokens", "global-calls", "global-tokens"])
def test_capability_requires_real_chat_authorization_and_remaining_budget(sandbox, capability, block):
    exp = sandbox.exp
    changes = {capability: True}
    if block == "no-chat":
        changes["pages"] = ["models"]
    if block == "grant-calls":
        changes["usedCalls"] = 10
    if block == "grant-tokens":
        changes["usedTokens"] = 100000
    record = seed(sandbox, mode="public" if block == "public" else "real", **changes)
    with exp.database() as db:
        cfg = exp.config(db)
        exp.put(db, "usage", "usage", "global", {
            "calls": cfg["globalCalls"] if block == "global-calls" else 0,
            "tokens": cfg["globalTokens"] if block == "global-tokens" else 0,
        })
        state = exp.session_state(db, record.token)
        with pytest.raises(HTTPException) as caught:
            exp.require_capability(db, state, capability)
        assert caught.value.status_code == (403 if block in ("public", "no-chat") else 429)


async def test_authorized_upload_list_delete_and_cross_session_isolation(sandbox, client):
    record = seed(sandbox, allow_chat_attachments=True, allow_web_search=True)
    authenticate(client, record)

    async def parse(path, name, timeout, check):
        await check()
        assert path.is_relative_to(sandbox.root)
        assert name == "fixture.txt" and 0 < timeout <= 60
        return path.read_text(encoding="utf-8")

    sandbox.monkeypatch.setattr(sandbox.exp, "parse_attachment", parse)
    response = await client.post("/api/experience/attachments", content=b"fixture body",
                                 headers={"x-experience-filename": "fixture.txt"})
    assert response.status_code == 200, response.text
    attachment_id = response.json()["id"]
    path = sandbox.files.attachment_path(record.key, attachment_id, ".txt")
    assert path.read_bytes() == b"fixture body"
    assert [row["id"] for row in (await client.get("/api/experience/attachments")).json()] == [attachment_id]
    with sandbox.exp.database() as db:
        state = sandbox.exp.session_state(db, record.token)
        assert "fixture body" in sandbox.exp.attachment_text(db, state, [attachment_id])
        sandbox.exp.require_capability(db, state, "allow_web_search")
    other = seed(sandbox, token="second-token", allow_chat_attachments=True)
    authenticate(client, other)
    assert (await client.get("/api/experience/attachments")).json() == []
    with sandbox.exp.database() as db:
        with pytest.raises(HTTPException) as caught:
            sandbox.exp.attachment_text(db, sandbox.exp.session_state(db, other.token), [attachment_id])
        assert caught.value.status_code == 403
    assert (await client.delete("/api/experience/attachments/" + attachment_id)).status_code == 200
    assert path.exists()
    authenticate(client, record)
    assert (await client.delete("/api/experience/attachments/" + attachment_id)).status_code == 200
    assert not path.exists()


@pytest.mark.parametrize("outcome", ["failure", "expired", "revoked", "capability-removed"])
async def test_upload_rechecks_access_after_parse_and_removes_partial_files(sandbox, client, outcome):
    record = seed(sandbox, allow_chat_attachments=True)
    authenticate(client, record)

    async def parse(path, name, timeout, check):
        if outcome == "failure":
            raise ValueError("synthetic parser failure")
        with sandbox.exp.database() as db:
            grant = sandbox.exp.load(db, record.grant_id)
            if outcome == "expired":
                grant["expires"] = sandbox.exp.time.time() - 1
            elif outcome == "revoked":
                grant["revoked"] = True
            else:
                grant["allow_chat_attachments"] = False
            sandbox.exp.put(db, record.grant_id, "grant", "1", grant)
        return "parsed fixture"

    sandbox.monkeypatch.setattr(sandbox.exp, "parse_attachment", parse)
    response = await client.post("/api/experience/attachments", content=b"fixture",
                                 headers={"x-experience-filename": "fixture.txt"})
    assert response.status_code == {"failure": 422, "expired": 401, "revoked": 401, "capability-removed": 403}[outcome]
    with sandbox.exp.database() as db:
        assert db.execute("SELECT count(*) FROM ephemeral.attachments").fetchone()[0] == 0
    assert not list(sandbox.files.session_directory(record.key).glob("*.txt"))


@pytest.mark.parametrize("reason", ["expired", "revoked", "ended"])
async def test_expiry_revoke_and_leave_remove_content_credentials_and_files(sandbox, client, reason):
    record = seed(sandbox, allow_chat_attachments=True, usedCalls=2, usedTokens=1234)
    _, path = add_content(sandbox, record)
    authenticate(client, record)
    cancelled = []
    sandbox.monkeypatch.setattr(sandbox.exp, "cancel_running", cancelled.append)
    with sandbox.exp.database() as db:
        sandbox.exp.put(db, "usage", "usage", "global", {"calls": 7, "tokens": 8000})
    if reason == "expired":
        with sandbox.exp.database() as db:
            grant = sandbox.exp.load(db, record.grant_id)
            grant["expires"] = sandbox.exp.time.time() - 1
            sandbox.exp.put(db, record.grant_id, "grant", "1", grant)
        assert (await client.get("/api/experience/session")).status_code == 401
        sandbox.exp.cleanup()
    elif reason == "revoked":
        assert (await client.delete("/api/ExperienceAdmin/Grants/" + record.grant_id)).status_code == 200
    else:
        assert (await client.post("/api/experience/leave")).status_code == 200
    assert_cleaned(sandbox, record, path, reason)
    assert record.key in cancelled
    assert (await client.get("/api/experience/session")).status_code == 401
    sandbox.exp.cleanup()
    assert_cleaned(sandbox, record, path, reason)


async def test_admin_role_and_tenant_cannot_revoke_someone_elses_grant(sandbox, client):
    record = seed(sandbox, allow_chat_attachments=True)
    _, path = add_content(sandbox, record)
    sandbox.principal.is_super_admin = False
    assert (await client.delete("/api/ExperienceAdmin/Grants/" + record.grant_id)).status_code == 403
    sandbox.principal.is_super_admin = True
    sandbox.principal.tenant_id = 2
    assert (await client.delete("/api/ExperienceAdmin/Grants/" + record.grant_id)).status_code == 404
    assert path.exists()
    with sandbox.exp.database() as db:
        assert sandbox.exp.load(db, record.key) is not None


def test_cleanup_preserves_active_session_and_removes_only_generated_orphans(sandbox):
    active = seed(sandbox)
    _, active_path = add_content(sandbox, active)
    orphan = sandbox.files.session_directory("session:" + "b" * 64)
    orphan.mkdir(parents=True)
    (orphan / "fixture.txt").write_bytes(b"orphan")
    unrelated = sandbox.files.sessions_root() / "operator-notes"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_bytes(b"keep")
    sandbox.exp.cleanup()
    assert active_path.exists()
    assert not orphan.exists()
    assert (unrelated / "keep.txt").read_bytes() == b"keep"


@pytest.mark.parametrize("capability", CAPABILITIES)
def test_authorization_is_rechecked_before_external_budget_reservation(sandbox, capability):
    exp = sandbox.exp
    record = seed(sandbox, **{capability: True})
    with exp.database() as db:
        state = exp.session_state(db, record.token)
        exp.reserve(db, state, 100)
        state["token"] = record.token
        exp.reserve_external(db, state, 20, capability)
        assert exp.load(db, "usage") == {"calls": 2, "tokens": 120}
        grant = exp.load(db, record.grant_id)
        grant[capability] = False
        exp.put(db, record.grant_id, "grant", "1", grant)
        with pytest.raises(HTTPException) as caught:
            exp.reserve_external(db, state, 20, capability)
        assert caught.value.status_code == 403
        assert exp.load(db, "usage") == {"calls": 2, "tokens": 120}


async def test_admin_capability_removal_clears_session_payload_without_resetting_usage(sandbox, client):
    exp = sandbox.exp
    record = seed(sandbox, allow_chat_attachments=True, allow_web_search=True,
                  usedCalls=2, usedTokens=1234)
    _, path = add_content(sandbox, record)
    authenticate(client, record)
    cancelled = []
    sandbox.monkeypatch.setattr(exp, "cancel_running", cancelled.append)

    @asynccontextmanager
    async def synthetic_session():
        yield object()

    async def validate(grant, session, **kwargs):
        assert grant["tenant"] == "1" and kwargs == {"allow_rebind": True}
        grant["knowledgeModels"] = {}
        return [], [], []

    sandbox.monkeypatch.setattr(exp, "SessionLocal", synthetic_session)
    sandbox.monkeypatch.setattr(exp, "validate_resources", validate)
    payload = {name: record.grant[name] for name in (
        "revision", "minutes", "claimEnd", "hardEnd", "expires", "calls", "tokens",
        "pages", "modelIds", "agentIds", "knowledgeIds")}
    # 更新时省略能力字段应回到默认拒绝，不能继承旧授权。
    response = await client.put("/api/ExperienceAdmin/Grants/" + record.grant_id, json=payload)
    assert response.status_code == 200, response.text
    current = await client.get("/api/experience/session")
    assert current.status_code == 200
    assert all(current.json()[name] is False for name in CAPABILITIES)
    assert current.json()["messages"] == [] and current.json()["attachments"] == []
    assert not path.parent.exists() and record.key in cancelled
    with exp.database() as db:
        grant = exp.load(db, record.grant_id)
        assert grant["revision"] == 2 and grant["usedCalls"] == 2 and grant["usedTokens"] == 1234
        assert db.execute("SELECT count(*) FROM ephemeral.attachments").fetchone()[0] == 0
    # 旧 revision 不得再次修改资格。
    assert (await client.put("/api/ExperienceAdmin/Grants/" + record.grant_id, json=payload)).status_code == 409


@pytest.mark.parametrize("outcome", ["success", "parser-error", "access-revoked", "timeout"])
async def test_parser_process_is_reaped_on_completion_failure_or_revocation(file_sandbox, outcome):
    files = file_sandbox.files
    finished = asyncio.Event()
    process = SimpleNamespace(returncode=None, killed=False, waited=False)

    async def communicate():
        if outcome in ("timeout", "access-revoked"):
            await finished.wait()
        else:
            process.returncode = 0 if outcome == "success" else 1
        return b'{"text":"synthetic parsed text"}', b""

    def kill():
        process.killed = True
        process.returncode = -1
        finished.set()

    async def wait():
        process.waited = True
        return process.returncode

    process.communicate, process.kill, process.wait = communicate, kill, wait

    async def spawn(*args, **kwargs):
        assert args[1:3] == ("-m", "eic.experience_parser")
        return process

    async def check():
        if outcome == "access-revoked":
            raise HTTPException(403, "synthetic revoked access")

    file_sandbox.monkeypatch.setattr(files.asyncio, "create_subprocess_exec", spawn)
    path = file_sandbox.root / "synthetic.txt"
    if outcome == "success":
        assert await files.parse_attachment(path, "synthetic.txt", 1, check) == "synthetic parsed text"
        assert not process.killed
    else:
        error = {"parser-error": ValueError, "access-revoked": HTTPException, "timeout": TimeoutError}[outcome]
        with pytest.raises(error):
            await files.parse_attachment(path, "synthetic.txt", 0.01 if outcome == "timeout" else 1, check)
        if outcome != "parser-error":
            assert process.killed and process.waited


@pytest.mark.parametrize("key", ["", "../outside", "session:" + "g" * 64, "session:" + "a" * 63])
def test_session_paths_reject_non_generated_keys(file_sandbox, key):
    with pytest.raises(ValueError):
        file_sandbox.files.session_directory(key)


@pytest.mark.parametrize("attachment_id,suffix", [
    ("../outside", ".txt"), ("attachment:" + "a" * 32, "/outside"),
    ("attachment:" + "a" * 32, ".TXT"), ("attachment:" + "a" * 31, ".txt"),
])
def test_attachment_paths_reject_traversal_and_invalid_identifiers(file_sandbox, attachment_id, suffix):
    with pytest.raises(ValueError):
        file_sandbox.files.attachment_path("session:" + "a" * 64, attachment_id, suffix)


def test_legacy_migration_defaults_deny_preserves_explicit_grants_and_is_idempotent(file_sandbox):
    from eic.experience_migrations import migrate

    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("CREATE TABLE records (id TEXT PRIMARY KEY, kind TEXT, tenant TEXT, data TEXT)")
        rows = [
            ("grant:old", "grant", "1", json.dumps({"visible_pages": ["chat", "models"]})),
            ("grant:explicit", "grant", "1", json.dumps({"pages": ["chat"], "allow_chat_attachments": True, "allow_web_search": True})),
            ("grant:broken", "grant", "1", "not-json"),
            ("grant:list", "grant", "1", "[]"),
            ("session:untouched", "session", "ephemeral", json.dumps({"fixture": "unchanged"})),
        ]
        db.executemany("INSERT INTO records VALUES (?,?,?,?)", rows)
        migrate(db)
        snapshot = list(db.execute("SELECT * FROM records ORDER BY id"))
        data = {row["id"]: row["data"] for row in snapshot}
        legacy = json.loads(data["grant:old"])
        assert legacy["pages"] == ["chat", "models"]
        assert all(legacy[name] is False for name in CAPABILITIES)
        assert all(json.loads(data["grant:explicit"])[name] is True for name in CAPABILITIES)
        assert data["grant:broken"] == "not-json" and data["grant:list"] == "[]"
        assert json.loads(data["session:untouched"]) == {"fixture": "unchanged"}
        assert json.loads(data["experience-schema"])["version"] == 2
        migrate(db)
        assert [tuple(row) for row in db.execute("SELECT * FROM records ORDER BY id")] == [tuple(row) for row in snapshot]

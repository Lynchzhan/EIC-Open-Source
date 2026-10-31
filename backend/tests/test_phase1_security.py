"""Phase 1 回归源码；不在 collection 时导入应用或修改环境。"""

import asyncio
import os
import platform
import socket
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest
from fastapi import BackgroundTasks


def deny_real_io(*args, **kwargs):
    raise AssertionError("Phase 1 fixture forbids real network, DNS, database, and subprocess I/O")


def patch_route_dependency(monkeypatch, endpoint, name, replacement):
    from dataclasses import replace
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__ or ()))
    if name not in cells and name in endpoint.__globals__:
        monkeypatch.setitem(endpoint.__globals__, name, replacement)
        return
    if name not in cells and "dependencies" in cells:
        cell = cells["dependencies"]
        assert hasattr(cell.cell_contents, name), f"Route dependency not found: {name}"
        monkeypatch.setattr(cell, "cell_contents", replace(cell.cell_contents, **{name: replacement}))
        return
    assert name in cells, f"Route closure dependency not found: {name}"
    monkeypatch.setattr(cells[name], "cell_contents", replacement)


def patch_main_dependency(monkeypatch, module, name, replacement):
    """同时替换公开入口和装配时捕获的依赖，保持授权夹具无真实 I/O。"""
    from dataclasses import is_dataclass, replace
    from inspect import isfunction

    original = getattr(module, name)
    for endpoint in tuple(vars(module).values()):
        if not isfunction(endpoint):
            continue
        if endpoint.__globals__.get(name) is original:
            monkeypatch.setitem(endpoint.__globals__, name, replacement)
        cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__ or ()))
        if name in cells:
            monkeypatch.setattr(cells[name], "cell_contents", replacement)
        if "dependencies" in cells:
            cell = cells["dependencies"]
            dependency = cell.cell_contents
            if is_dataclass(dependency) and hasattr(dependency, name):
                monkeypatch.setattr(cell, "cell_contents", replace(dependency, **{name: replacement}))
    monkeypatch.setattr(module, name, replacement)


@pytest.fixture(scope="module")
def main_module(tmp_path_factory):
    import sys
    from importlib import import_module

    if any(name == "eic" or name.startswith("eic.") for name in sys.modules):
        raise RuntimeError("Run this module in a fresh isolated pytest process; application configuration was already imported")
    # Windows 的平台信息探测可能调用子进程，提前缓存后再启用所有 I/O 禁令。
    platform.uname()
    # MCP Windows 模块在导入时用 Popen[bytes] 注解，先导入再禁止进程 I/O。
    import mcp
    root = tmp_path_factory.mktemp("phase1-security")
    mp = pytest.MonkeyPatch()
    try:
        # Settings 的相对 .env 只在独立临时目录查找，不读取 checkout 配置。
        mp.chdir(root)
        for name in tuple(os.environ):
            if name.upper().startswith("EIC_"):
                mp.delenv(name, raising=False)
        environment = {
            "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + (root / "unused.db").as_posix(),
            "EIC_UPLOAD_ROOT": str(root / "uploads"),
            "EIC_SKILL_ROOT": str(root / "skills"),
            "EIC_FRONTEND_DIST": str(root / "frontend-dist"),
            "EIC_SEED_DEMO_DATA": "false",
            "EIC_AUTO_CREATE_SCHEMA": "false",
            "EIC_REQUIRE_RUNTIME_DEPENDENCIES": "false",
            "EIC_SERVE_FRONTEND": "false",
            "EIC_BOOTSTRAP_ADMIN_PASSWORD": "",
            "EIC_DEMO_USER_PASSWORD": "",
            "EIC_REDIS_URL": "redis://fixture.invalid:6379/0",
            "EIC_VECTOR_DATABASE_URL": "postgresql+asyncpg://fixture:fixture@fixture.invalid/eic",
            "EIC_QDRANT_URL": "http://fixture.invalid:6333",
            "EIC_CORS_ORIGINS": "http://fixture.invalid",
            "EIC_ENVIRONMENT": "testing",
            "EIC_JWT_SECRET": "phase1-test-only-jwt-secret-at-least-32-characters",
            "EIC_SECRET_ENCRYPTION_KEY": "phase1-test-only-encryption-secret-at-least-32-characters",
        }
        for name, value in environment.items():
            mp.setenv(name, value)
        mp.setattr(socket, "getaddrinfo", deny_real_io)
        mp.setattr(socket, "create_connection", deny_real_io)
        # 不拦截原始 socket.connect：Windows 事件循环的内部 socketpair 需要它。
        mp.setattr(asyncio.BaseEventLoop, "create_connection", deny_real_io)
        mp.setattr(asyncio.BaseEventLoop, "sock_connect", deny_real_io)
        mp.setattr(subprocess, "Popen", deny_real_io)
        mp.setattr(os, "system", deny_real_io)

        import asyncpg
        import httpx
        from sqlalchemy.ext.asyncio import AsyncEngine

        # MockTransport 不受影响；真实 HTTP transport 和数据库连接均直接失败。
        mp.setattr(asyncpg, "connect", deny_real_io)
        mp.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_real_io)
        mp.setattr(httpx.HTTPTransport, "handle_request", deny_real_io)
        mp.setattr(AsyncEngine, "connect", deny_real_io)
        mp.setattr(AsyncEngine, "begin", deny_real_io)
        module = import_module("eic.main")
        async def fixture_creation_lock(_session, _tenant_id):
            # 本文件只测纯授权/规范化；真实事务锁由双进程 HTTP 验收覆盖。
            return None
        patch_main_dependency(mp, module, "lock_tenant_writes", fixture_creation_lock)
        mp.setattr(module.model_lifecycle, "lock_tenant", fixture_creation_lock)
        # 本文件隔离授权/绑定与提交顺序；持久 outbox 由真实数据库故障测试覆盖。
        mp.setattr(module.outbox, "stage_document", AsyncMock(return_value=None))
        assert Path(module.__file__).resolve() == Path(__file__).resolve().parents[1] / "src" / "eic" / "main.py"
        database = sys.modules["eic.core.database"]
        mp.setattr(module, "SessionLocal", deny_real_io)
        mp.setattr(database, "SessionLocal", deny_real_io)
        yield module
    finally:
        mp.undo()
        for name in tuple(sys.modules):
            if name == "eic" or name.startswith("eic."):
                sys.modules.pop(name, None)


def test_fixture_configuration_has_no_checkout_runtime(main_module):
    module = main_module
    root = Path.cwd()
    assert module.settings.environment == "testing"
    assert module.settings.upload_root == str(root / "uploads")
    assert module.settings.skill_root == str(root / "skills")
    assert module.settings.frontend_dist == str(root / "frontend-dist")
    assert module.settings.database_url == "sqlite+aiosqlite:///" + (root / "unused.db").as_posix()
    assert not (root / ".env").exists() and not (root / "unused.db").exists()
    assert not module.settings.seed_demo_data and not module.settings.auto_create_schema
    assert not module.settings.serve_frontend and not module.settings.require_runtime_dependencies
    assert not module.settings.bootstrap_admin_password and not module.settings.demo_user_password


class FakeSession:
    def __init__(self, records=None, rows=None):
        self.records = records or {}
        self.info = {}
        self.rows = rows or []
        self.commits = 0
        self.deleted = []
        self.no_autoflush = nullcontext()
        self.allocator_high = 2_000_000_000_000_000

    @staticmethod
    def is_allocator_query(statement):
        columns = list(statement.selected_columns)
        return (len(columns) == 1 and columns[0].name == "value"
                and columns[0].table.name == "id_allocator")

    async def scalar(self, statement):
        # 只模拟 ID 校验查询；真实分配事务另由真实数据库测试覆盖。
        assert self.is_allocator_query(statement), "Unexpected scalar query in authorization fixture"
        return self.allocator_high

    async def get(self, model, item_id, **kwargs):
        return self.records.get((model.__name__, item_id))

    async def scalars(self, statement):
        return self.rows

    async def commit(self):
        self.commits += 1

    async def delete(self, item):
        self.deleted.append(item)


def principal(module, user=10, tenant=1):
    return module.Principal(user, tenant, "fixture-user", False)


def chat_records(owner=10, tenant=1, request_id="request-fixture"):
    return {
        ("ChatSession", 500): SimpleNamespace(id=500, tenant_id=tenant, owner_id=owner),
        ("Resource", 500): SimpleNamespace(id=500, tenant_id=tenant, owner_id=owner, resource_type="chats", is_deleted=False),
        ("Resource", 900): SimpleNamespace(
            id=900, tenant_id=tenant, owner_id=owner, resource_type="chat_messages", is_deleted=False,
            data={"content": "fixture", "aIChatsId": 500}, name="fixture",
        ),
    }


@pytest.mark.parametrize("active", [True, False])
def test_old_token_rechecks_tenant_state(main_module, monkeypatch, active):
    module = main_module
    from auth_fixture import signed_token
    claims = module.decode_access_token(signed_token(10, 1, "fixture", "synthetic-hash"))
    patch_main_dependency(monkeypatch, module, "decode_access_token", lambda _: claims)
    session = FakeSession({
        ("User", 10): SimpleNamespace(id=10, tenant_id=1, is_active=True, username="fixture", is_super_admin=False, password_hash="synthetic-hash", approval_status="approved"),
        ("Tenant", 1): SimpleNamespace(id=1, active=active),
    })
    if active:
        assert asyncio.run(module.current_principal("Bearer fixture", session)).user_id == 10
    else:
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.current_principal("Bearer fixture", session))
        assert error.value.status_code == 401


@pytest.mark.parametrize("owner,tenant", [(10, 1), (11, 1), (10, 2), (None, 1)])
def test_request_owner_checks_include_non_numeric_ids(main_module, owner, tenant):
    module = main_module
    stored = SimpleNamespace(id=900, session_id=500, tenant_id=tenant, role="user")
    session = FakeSession(chat_records(owner, tenant), [stored])
    if owner == 10 and tenant == 1:
        resource, message = asyncio.run(module.require_owned_chat_request(session, principal(module), "request-fixture"))
        assert resource.id == message.id == 900
    else:
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.require_owned_chat_request(session, principal(module), "request-fixture"))
        assert error.value.status_code == 404


def test_ambiguous_request_is_denied(main_module):
    module = main_module
    session = FakeSession(chat_records(), [
        SimpleNamespace(id=900, session_id=500), SimpleNamespace(id=901, session_id=500),
    ])
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.require_owned_chat_request(session, principal(module), "duplicate"))
    assert error.value.status_code == 404


@pytest.mark.parametrize("owner,tenant", [(11, 1), (10, 2), (None, 1)])
def test_task_status_and_cancel_deny_other_owners(main_module, monkeypatch, owner, tenant):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    task = SimpleNamespace(id=100, tenant_id=tenant, owner_id=owner, enabled=True, status="ready")
    session = FakeSession({("ScheduledTask", 100): task})
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.task_status(100, principal(module), session))
    assert error.value.status_code == 404
    assert asyncio.run(module.task_cancel(100, principal(module), session)) is False
    assert task.enabled and task.status == "ready" and session.commits == 0


def test_deleting_other_users_message_has_no_side_effect(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    records = chat_records(owner=11)
    records[("ChatMessage", 900)] = SimpleNamespace(id=900, tenant_id=1, session_id=500)
    session = FakeSession(records)
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.chat_history_delete(900, principal(module), session))
    assert error.value.status_code == 404
    assert not session.deleted and session.commits == 0


@pytest.mark.parametrize("resource_type,owner,tenant", [
    ("ai_apps", 10, 1), ("chat_messages", 11, 1), ("chat_messages", 10, 2),
])
def test_forged_message_id_does_not_overwrite_resource(main_module, monkeypatch, resource_type, owner, tenant):
    from fastapi import Response

    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    item = SimpleNamespace(id=900, tenant_id=tenant, owner_id=owner, resource_type=resource_type,
                           is_deleted=False, name="original", data={"preserve": True})
    session = FakeSession({("Resource", 900): item})
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.chat_history_add({"id": "900", "content": "new"}, None, Response(), principal(module), session))
    assert error.value.status_code == 409
    assert item.name == "original" and item.data == {"preserve": True} and session.commits == 0


@pytest.mark.parametrize("changed", [False, True])
def test_identical_non_numeric_retry_is_immutable(main_module, monkeypatch, changed):
    from fastapi import Response

    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    stored = SimpleNamespace(id=900, session_id=500, tenant_id=1, role="user", content="fixture")
    session = FakeSession(chat_records(), [stored])
    request = SimpleNamespace(url=SimpleNamespace(scheme="http"))
    payload = {"id": "request-fixture", "content": "changed" if changed else "fixture", "aIChatsId": 500}
    if changed:
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.chat_history_add(payload, request, Response(), principal(module), session))
        assert error.value.status_code == 409
    else:
        result = asyncio.run(module.chat_history_add(payload, request, Response(), principal(module), session))
        assert result["id"] == "request-fixture" and result["aIChatsId"] == 500
    assert session.records[("Resource", 900)].data["content"] == "fixture" and session.commits == 0


@pytest.mark.parametrize("active,tenant", [(True, 1), (False, 1), (True, 2)])
def test_organization_reference_requires_active_same_tenant(main_module, active, tenant):
    module = main_module
    session = FakeSession({("Department", 200): SimpleNamespace(id=200, tenant_id=tenant, active=active)})
    if active and tenant == 1:
        assert asyncio.run(module.validate_organization_reference(module.Department, 200, principal(module), session)) == 200
    else:
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.validate_organization_reference(module.Department, 200, principal(module), session))
        assert error.value.status_code == 400


@pytest.mark.parametrize("enabled,model_type,tenant", [
    (False, "embedding", 1), (True, "chat", 1), (True, "embedding", 2),
])
def test_invalid_embedding_model_makes_zero_provider_requests(main_module, enabled, model_type, tenant):
    import httpx
    from eic.ai.embeddings import embed_texts
    from eic.ai.providers import ModelProviderError

    calls = []
    model = main_module.AIModel(id=1, tenant_id=tenant, model_type=model_type, enabled=enabled,
                               endpoint="http://fixture.invalid/v1", model_name="fixture", secret_encrypted=None)
    async def run():
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ModelProviderError):
                await embed_texts(model, ["fixture"], tenant_id=1, client=client)
    asyncio.run(run())
    assert calls == []


@pytest.mark.parametrize("payload", [
    {"embeddingModelId": 1}, {"aIModelsId": 1, "embeddingModelId": 2},
])
def test_model_binding_rejects_cross_tenant_or_conflicting_aliases(main_module, payload):
    from eic.knowledge.ops import validate_knowledge_model_bindings

    session = FakeSession({("AIModel", 1): SimpleNamespace(tenant_id=2, enabled=True, model_type="embedding")})
    with pytest.raises(main_module.HTTPException) as error:
        asyncio.run(validate_knowledge_model_bindings(session, 1, payload))
    assert error.value.status_code == 400


@pytest.mark.parametrize("approval", ["pending", "rejected"])
def test_active_user_with_unapproved_status_is_denied(main_module, monkeypatch, approval):
    module = main_module
    patch_main_dependency(monkeypatch, module, "decode_access_token", lambda _: {"sub": "10", "tenant_id": 1})
    session = FakeSession({
        ("User", 10): SimpleNamespace(id=10, tenant_id=1, is_active=True, approval_status=approval),
    })
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.current_principal("Bearer fixture", session))
    assert error.value.status_code == 401


def test_chat_request_normalizes_aliases_and_ignores_server_cancel_flag(main_module):
    module = main_module
    original = module.normalize_chat_request_data({"content": "fixture", "appId": 100, "cancelled": True}, 500)
    retried = module.normalize_chat_request_data({"content": "fixture", "aIAppsId": "100"}, 500)
    assert original == retried == {"content": "fixture", "aIChatsId": 500, "appId": 100}
    with pytest.raises(module.HTTPException) as error:
        module.normalize_chat_request_data({"appId": 100, "aIAppsId": 101}, 500)
    assert error.value.status_code == 400


@pytest.mark.parametrize("deleted", [False, True])
def test_deleted_session_blocks_generation_but_allows_owned_history(main_module, deleted):
    module = main_module
    records = chat_records()
    records[("Resource", 500)].is_deleted = deleted
    stored = SimpleNamespace(id=900, session_id=500, tenant_id=1, role="user")
    session = FakeSession(records, [stored])
    assert asyncio.run(module.require_owned_chat_request(session, principal(module), "fixture", allow_deleted=True))[1] is stored
    if deleted:
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.require_owned_chat_request(session, principal(module), "fixture"))
        assert error.value.status_code == 404
    else:
        assert asyncio.run(module.require_owned_chat_request(session, principal(module), "fixture"))[1] is stored


def test_owner_message_delete_tombstones_matching_resource(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    clear_context = AsyncMock()
    monkeypatch.setattr(module.memory_lifecycle, "clear_context", clear_context)
    records = chat_records()
    records[("ChatMessage", 900)] = SimpleNamespace(id=900, tenant_id=1, session_id=500)
    session = FakeSession(records)
    assert asyncio.run(module.chat_history_delete(900, principal(module), session)) is True
    assert records[("Resource", 900)].is_deleted and session.commits == 1
    assert session.deleted == [records[("ChatMessage", 900)]]
    clear_context.assert_awaited_once_with(session, 1, 10, 500)


@pytest.mark.parametrize("value", [False, True, -1, 2**63])
def test_invalid_organization_id_rejected_before_database_lookup(main_module, value):
    module = main_module
    session = FakeSession()
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.validate_organization_reference(module.Department, value, principal(module), session))
    assert error.value.status_code == 400


def test_organization_alias_conflict_and_invalid_secondary_position_are_rejected(main_module):
    module = main_module
    records = {
        ("Department", 200): SimpleNamespace(id=200, tenant_id=1, active=True),
        ("Department", 201): SimpleNamespace(id=201, tenant_id=1, active=True),
        ("Position", 300): SimpleNamespace(id=300, tenant_id=1, active=True),
        ("Position", 301): SimpleNamespace(id=301, tenant_id=2, active=True),
    }
    for payload in (
        {"departmentId": 200, "dtoUserInfo": {"departmentId": 201}},
        {"positions": [{"id": 300}, {"id": 301}]},
        {"positions": False},
    ):
        user = SimpleNamespace(department_id=None, position_id=None)
        session = FakeSession(records)
        with pytest.raises(module.HTTPException) as error:
            asyncio.run(module.persist_user_profile(user, payload, principal(module), session))
        assert error.value.status_code == 400 and session.commits == 0


def test_valid_model_binding_and_embedding_provider_mock(main_module):
    import httpx
    from eic.ai.embeddings import embed_texts
    from eic.knowledge.ops import validate_knowledge_model_bindings

    model = main_module.AIModel(id=1, tenant_id=1, model_type="embedding", enabled=True,
                               provider="openai_compatible",
                               endpoint="http://fixture.invalid/v1", model_name="fixture",
                               embedding_dimensions=1, secret_encrypted=None)
    session = FakeSession({("AIModel", 1): model})
    asyncio.run(validate_knowledge_model_bindings(session, 1, {"aIModelsId": 1, "embeddingModelId": "1"}))
    calls = []
    async def run():
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await embed_texts(model, ["fixture"], tenant_id=1, client=client)
    assert asyncio.run(run()) == [[1.0]] and len(calls) == 1


class PolicySession(FakeSession):
    def __init__(self, records=None, rows=None, scalar_value=None, dialect="postgresql"):
        super().__init__(records, rows)
        self.scalar_value = scalar_value
        self.added = []
        self.flushes = 0
        self.get_calls = []
        self.dialect = dialect
        self.execute_calls = []
        self.operations = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    async def execute(self, statement, parameters=None):
        self.operations.append("execute")
        self.execute_calls.append((str(statement), parameters))

    async def get(self, model, item_id, **kwargs):
        self.operations.append("get")
        self.get_calls.append((model.__name__, item_id, kwargs))
        return await super().get(model, item_id, **kwargs)

    async def scalar(self, statement):
        if self.is_allocator_query(statement):
            return await super().scalar(statement)
        return self.scalar_value

    def add(self, item):
        self.added.append(item)
        self.records[(type(item).__name__, item.id)] = item

    async def flush(self):
        self.flushes += 1


def policy_file(item_id=700, mode="private", owner=10, tenant=1, deleted=False, kind="files"):
    data = {"size": 7, "path": "untrusted-path"}
    if mode != "legacy":
        data["accessMode"] = mode
    return SimpleNamespace(id=item_id, name=f"fixture-{item_id}.txt", tenant_id=tenant,
                           owner_id=owner, resource_type=kind, is_deleted=deleted, data=data)


@pytest.mark.parametrize("mode,owner,tenant,deleted,kind,read,write,bind", [
    ("private", 10, 1, False, "files", True, True, False),
    ("private", 11, 1, False, "files", False, False, False),
    ("private", None, 1, False, "files", False, False, False),
    ("tenant", 11, 1, False, "files", True, False, True),
    ("tenant", 10, 1, False, "files", True, True, True),
    ("tenant", None, 1, False, "files", True, False, True),
    ("legacy", None, 1, False, "files", True, True, True),
    ("legacy", 11, 1, False, "files", True, True, True),
    ("tenant", 10, 2, False, "files", False, False, False),
    ("tenant", 10, 1, True, "files", False, False, False),
    ("tenant", 10, 1, False, "skills", False, False, False),
    ("public", 10, 1, False, "files", False, False, False),
    (None, 10, 1, False, "files", False, False, False),
])
def test_file_policy_matrix(main_module, mode, owner, tenant, deleted, kind, read, write, bind):
    from eic.core.file_access import can_access_file, can_bind_shared_file

    item = policy_file(mode=mode, owner=owner, tenant=tenant, deleted=deleted, kind=kind)
    assert can_access_file(item, 1, 10) is read
    assert can_access_file(item, 1, 10, write=True) is write
    assert can_bind_shared_file(item, 1) is bind


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, {}, [], None, "²", "١", "9" * 5000, 2**63])
def test_file_id_rejected_before_lookup(main_module, value):
    session = PolicySession()
    with pytest.raises(main_module.HTTPException) as error:
        asyncio.run(main_module.require_file_access(session, principal(main_module), value))
    assert error.value.status_code == 400 and not session.get_calls


@pytest.mark.parametrize("dialect", ["sqlite", "postgresql"])
@pytest.mark.parametrize("mode", ["read", "write", "shared"])
def test_file_transaction_lock_is_dialect_specific(main_module, dialect, mode):
    session = PolicySession({("Resource", 700): policy_file(mode="tenant")}, dialect=dialect)
    result = asyncio.run(main_module.require_file_access(
        session, principal(main_module), 700, write=mode == "write", shared=mode == "shared"))
    assert result.id == 700 and session.commits == 0
    if dialect == "sqlite" and mode != "read":
        assert session.operations == ["execute", "get"]
        assert session.execute_calls == [(
            "UPDATE resources SET id = id WHERE id = :file_id AND tenant_id = :tenant_id AND resource_type = 'files'",
            {"file_id": 700, "tenant_id": 1},
        )]
    else:
        assert session.operations == ["get"] and not session.execute_calls


def test_invalid_file_write_id_has_no_sqlite_statement(main_module):
    session = PolicySession(dialect="sqlite")
    with pytest.raises(main_module.HTTPException) as error:
        asyncio.run(main_module.require_file_access(session, principal(main_module), True, write=True))
    assert error.value.status_code == 400 and not session.operations


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/internal", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/internal", "http://fixture.invalid/redirect", "file:///fixture", "https://fixture.invalid/file",
])
def test_remote_import_disabled_has_zero_io(main_module, monkeypatch, enabled, url):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    monkeypatch.setattr(module.settings, "remote_file_import_enabled", enabled)
    counters = {"http": 0, "dns": 0, "file": 0}

    def blocked(label):
        def fail(*args, **kwargs):
            counters[label] += 1
            raise AssertionError(label)
        return fail

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", blocked("http"))
    monkeypatch.setattr(socket, "getaddrinfo", blocked("dns"))
    monkeypatch.setattr(Path, "write_bytes", blocked("file"))
    session = PolicySession()
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.file_remote_upload({"url": url}, principal(module), session))
    assert error.value.status_code == 403
    assert counters == {"http": 0, "dns": 0, "file": 0}
    assert session.commits == session.flushes == 0 and not session.added


def test_file_list_counts_only_visible_files(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    rows = [policy_file(), policy_file(701, owner=11), policy_file(702, mode="tenant", owner=11),
            policy_file(703, mode="legacy", owner=None), policy_file(704, tenant=2)]
    class FilePageSession(PolicySession):
        async def scalar(self, statement):
            compiled = statement.compile()
            assert "resources.tenant_id" in str(compiled)
            assert "resources.owner_id" in str(compiled)
            assert "accessMode" in compiled.params.values()
            return 3

        async def scalars(self, statement):
            assert statement._offset_clause.value == 0
            assert statement._limit_clause.value == 2
            return [rows[0], rows[2]]

    page = asyncio.run(module.file_page(module.PageInput(pageNum=1, pageSize=2), principal(module), FilePageSession(rows=rows)))
    assert page["total"] == 3 and [item["id"] for item in page["data"]] == [700, 702]
    assert all("path" not in item for item in page["data"])


@pytest.mark.parametrize("admin", [False, True])
def test_private_download_denied_before_path_or_response(main_module, monkeypatch, admin):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "file_source_path", deny_real_io)
    patch_main_dependency(monkeypatch, module, "FileResponse", deny_real_io)
    session = PolicySession({("Resource", 700): policy_file(owner=11)})
    actor = module.Principal(10, 1, "fixture", admin)
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.file_download(700, actor, session))
    assert error.value.status_code == 404 and session.commits == 0


@pytest.mark.parametrize("mode,owner", [("private", 10), ("tenant", 11), ("legacy", None)])
def test_visible_download_uses_checked_source(main_module, monkeypatch, mode, owner):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    calls = []
    patch_main_dependency(monkeypatch, module, "file_source_path", lambda item: calls.append(item.id) or Path("fixture.txt"))
    patch_main_dependency(monkeypatch, module, "FileResponse", lambda path, filename: (str(path), filename))
    session = PolicySession({("Resource", 700): policy_file(mode=mode, owner=owner)})
    assert asyncio.run(module.file_download(700, principal(module), session)) == ("fixture.txt", "fixture-700.txt")
    assert calls == [700]


@pytest.mark.parametrize("mode", ["private", "tenant", "legacy"])
@pytest.mark.parametrize("owner", [11, None])
def test_sharing_never_claims_another_or_missing_owner(main_module, monkeypatch, mode, owner):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", deny_real_io)
    item = policy_file(mode=mode, owner=owner)
    session = PolicySession({("Resource", 700): item})
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(module.file_set_sharing({"id": 700, "accessMode": "tenant"}, None, principal(module), session))
    assert error.value.status_code == 404 and session.commits == 0 and item.owner_id == owner


def test_owner_explicit_sharing_is_audited_and_locked(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    audits = []
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", AsyncMock(side_effect=lambda *args, **kwargs: audits.append(kwargs)))
    item = policy_file()
    session = PolicySession({("Resource", 700): item})
    result = asyncio.run(module.file_set_sharing({"id": 700, "accessMode": "tenant"}, None, principal(module), session))
    assert result["accessMode"] == "tenant" and "path" not in result and session.commits == 1
    assert audits[0]["table_id"] == 700 and audits[0]["payload"]["accessMode"] == "tenant"
    assert session.get_calls[0][2] == {"with_for_update": True, "populate_existing": True}


@pytest.mark.parametrize("operation", ["delete", "revoke"])
@pytest.mark.parametrize("reference", ["document", "skill", "kb-detail"])
def test_shared_reference_blocks_deletion_and_revoke(main_module, monkeypatch, operation, reference):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", deny_real_io)
    item = policy_file(mode="tenant")
    rows = []
    if reference == "skill":
        rows = [SimpleNamespace(data={"skillFile": {"id": "700"}})]
    elif reference == "kb-detail":
        rows = [SimpleNamespace(data={"aIKmssDetailsList": [{"file": {"id": 700}}]})]
    session = PolicySession({("Resource", 700): item}, rows, 800 if reference == "document" else None)
    with pytest.raises(module.HTTPException) as error:
        if operation == "delete":
            asyncio.run(module.file_delete(700, principal(module), session))
        else:
            asyncio.run(module.file_set_sharing({"id": 700, "accessMode": "private"}, None, principal(module), session))
    assert error.value.status_code == 409 and session.commits == 0
    assert not item.is_deleted and item.data["accessMode"] == "tenant"


@pytest.mark.parametrize("operation", ["save-kb", "import-kb", "skill"])
@pytest.mark.parametrize("owner,tenant,expected", [(10, 1, 400), (11, 1, 404), (10, 2, 404)])
def test_private_file_binding_rejected_before_writes(main_module, monkeypatch, operation, owner, tenant, expected):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "queue_or_run_document", deny_real_io)
    session = PolicySession({
        ("Resource", 700): policy_file(owner=owner, tenant=tenant),
        ("Resource", 600): SimpleNamespace(id=600, tenant_id=1, resource_type="knowledge_bases", is_deleted=False),
    })
    with pytest.raises(module.HTTPException) as error:
        if operation == "save-kb":
            asyncio.run(module.knowledge_base_add_edit({"id": 600, "fileIds": [700]}, BackgroundTasks(), principal(module), session))
        elif operation == "import-kb":
            asyncio.run(module.knowledge_import_file(600, 700, BackgroundTasks(), principal(module), session))
        else:
            asyncio.run(module.save_resource("skills", {"fileId": 700}, principal(module), session))
    assert error.value.status_code == expected
    assert not session.added and session.commits == session.flushes == 0


def test_private_file_replace_rejected_before_base_creation(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "has_permission", AsyncMock(return_value=True))
    endpoint = next(route.endpoint for route in module.app.routes if getattr(route, "path", "") == "/api/AIKmss/ReplaceDocumentFile")
    queue = AsyncMock(side_effect=deny_real_io)
    patch_route_dependency(monkeypatch, endpoint, "queue_or_run_document", queue)
    patch_route_dependency(monkeypatch, endpoint, "new_id", AsyncMock(return_value=900))
    document = SimpleNamespace(id=800, tenant_id=1, knowledge_base_id=600, file_id=701, is_deleted=False, source_revision=0)
    session = PolicySession({("KnowledgeDocument", 800): document, ("Resource", 700): policy_file()})
    with pytest.raises(module.HTTPException) as error:
        asyncio.run(endpoint(800, 700, BackgroundTasks(), principal(module), session))
    assert error.value.status_code == 400 and document.file_id == 701
    assert not session.added and session.commits == session.flushes == 0
    queue.assert_not_awaited()


@pytest.mark.parametrize("payload", [
    {"fileIds": "700"}, {"aIKmssDetailsList": [{"fileId": 700, "file": {"id": 701}}]},
    {"aIKmssDetailsList": [], "kmsDetailsList": [{"fileId": 700}]},
    {"aIKmssDetailsList": [True]}, {"aIKmssDetailsList": False},
])
def test_invalid_kb_file_aliases_rejected_without_writes(main_module, payload):
    session = PolicySession()
    with pytest.raises(main_module.HTTPException) as error:
        asyncio.run(main_module.validate_shared_file_payload(payload, principal(main_module), session))
    assert error.value.status_code == 400 and not session.get_calls and not session.added and session.commits == 0


@pytest.mark.parametrize("file_input", [{"fileId": 700}, {"skillFile": {"id": 700}}, {"fileId": "700", "skillFile": {"id": 700, "path": "/forged"}}])
def test_shared_skill_binding_persists_server_dto(main_module, monkeypatch, file_input):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", AsyncMock())
    session = PolicySession({("Resource", 700): policy_file(mode="tenant", owner=11)})
    assert asyncio.run(module.save_resource("skills", {"id": 600, "name": "fixture", **file_input}, principal(module), session))
    saved = session.records[("Resource", 600)].data
    assert saved["fileId"] == 700 and saved["skillFile"]["url"] == "/api/File/GetFile?fileid=700"
    assert "path" not in saved["skillFile"] and session.commits == 1


@pytest.mark.parametrize("file_input", [{"fileId": None}, {"skillFile": None}, {"fileId": None, "skillFile": None}])
def test_skill_binding_can_be_explicitly_cleared(main_module, monkeypatch, file_input):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", AsyncMock())
    skill = SimpleNamespace(id=600, tenant_id=1, resource_type="skills", is_deleted=False, name="fixture",
                            data={"fileId": 700, "skillFile": {"id": 700}})
    session = PolicySession({("Resource", 600): skill})
    asyncio.run(module.save_resource("skills", {"id": 600, "name": "fixture", **file_input}, principal(module), session))
    assert skill.data["fileId"] is None and skill.data["skillFile"] is None
    assert session.commits == 1 and len(session.get_calls) == 1


@pytest.mark.parametrize("payload", [
    {"contentFileUrls": "https://fixture.invalid/file"}, {"contentFileUrls": "//fixture.invalid/file"},
    {"contentFileUrls": "/api/File/GetFile?fileid=700&token=forged"},
    {"contentFileUrls": "/api/File/GetFile?fileid=700#fragment"},
    {"fileIds": [700], "contentFileUrls": "/api/File/GetFile?fileid=701"},
    {"attachments": [{"id": 700}]}, {"fileIds": [] , "fileNames": "forged"},
])
def test_chat_rejects_arbitrary_attachment_references(main_module, payload):
    session = PolicySession()
    with pytest.raises(main_module.HTTPException) as error:
        asyncio.run(main_module.normalize_chat_files(payload, principal(main_module), session))
    assert error.value.status_code == 400 and not session.get_calls and session.commits == 0


@pytest.mark.parametrize("mode,owner", [("private", 10), ("tenant", 11), ("legacy", None)])
def test_chat_attachment_names_and_urls_are_server_derived(main_module, monkeypatch, mode, owner):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    session = PolicySession({("Resource", 700): policy_file(mode=mode, owner=owner)})
    result = asyncio.run(module.normalize_chat_files({"fileIds": ["700", 700], "fileNames": "forged"}, principal(module), session))
    assert result["fileIds"] == [700] and result["fileNames"] == "fixture-700.txt"
    assert result["contentFileUrls"] == "/api/File/GetFile?fileid=700"


def test_empty_attachments_preserve_plain_request_and_canonicalize_explicit_empty(main_module):
    module = main_module
    session = PolicySession()
    plain = {"content": "fixture"}
    assert asyncio.run(module.normalize_chat_files(plain, principal(module), session)) == plain
    explicit = asyncio.run(module.normalize_chat_files({"fileIds": [], "contentFileUrls": None}, principal(module), session))
    assert explicit == {"fileIds": [], "fileNames": "", "contentFileUrls": ""}
    assert not session.get_calls


def test_sse_rechecks_attachment_before_provider(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    session = PolicySession({
        ("Resource", 700): policy_file(owner=11),
        ("User", 10): SimpleNamespace(id=10, tenant_id=1, username="fixture",
                                     is_active=True, is_super_admin=True, approval_status="approved"),
        ("Tenant", 1): SimpleNamespace(id=1, active=True),
    })

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    patch_main_dependency(monkeypatch, module, "SessionLocal", Context)
    patch_main_dependency(monkeypatch, module, "require_owned_chat_request", AsyncMock(return_value=(
        SimpleNamespace(tenant_id=1, data={"fileIds": [700]}), SimpleNamespace(content="fixture"),
    )))
    provider = AsyncMock()
    patch_main_dependency(monkeypatch, module, "stream_chat_completion", provider)

    async def run():
        stream = module._sse_events_inner("fixture", principal(module))
        try:
            await anext(stream)
        finally:
            await stream.aclose()

    with pytest.raises(module.HTTPException) as error:
        asyncio.run(run())
    assert error.value.status_code == 404 and provider.call_count == 0 and session.commits == 0


@pytest.mark.parametrize("message_state", ["missing", "foreign", "unconfigured"])
def test_sse_empty_state_uses_distinct_terminal_cursor(main_module, monkeypatch, message_state):
    module = main_module
    message = None if message_state == "missing" else SimpleNamespace(
        tenant_id=2 if message_state == "foreign" else 1, data={},
    )
    session = FakeSession({
        ("User", 10): SimpleNamespace(id=10, tenant_id=1, username="fixture",
                                     is_active=True, is_super_admin=True, approval_status="approved"),
        ("Tenant", 1): SimpleNamespace(id=1, active=True),
    })

    async def no_previous_run(statement):
        if statement.column_descriptions[0]["entity"] is module.Resource:
            assert statement.compile().params["resource_type_1"] == "tenant_chat_default"
            return None
        # 审批续跑的真实查询已在 P2 引入；本空态用例明确无旧 Run。
        assert statement.column_descriptions[0]["entity"] is module.AgentRun
        parameters = statement.compile().params
        assert parameters["tenant_id_1"] == 1
        assert parameters["request_id_1"] == "empty-state"
        return None

    monkeypatch.setattr(session, "scalar", no_previous_run)

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            pass

    recorded = []

    async def audited(request_id, actor, sequence, event_type, value, extra=None):
        recorded.append((sequence, event_type))
        return module.sse_message(request_id, sequence, event_type, value, extra)

    async def finalize(request_id, actor, **kwargs):
        # 此测试只检验空状态事件游标；真实事务和持久状态由 P6 集成测试覆盖。
        assert kwargs["status"] == "failed"
        assert kwargs["reason"] == "model_not_configured"
        assert kwargs["token_usage"]["providerCalls"] == 0
        chunks = []
        for event in ("generationerror", "done"):
            sequence = recorded[-1][0] + 1
            chunks.append(await audited(request_id, actor, sequence, event, "", {
                "status": "failed", "reason": "model_not_configured",
            }))
        return chunks

    patch_main_dependency(monkeypatch, module, "SessionLocal", Context)
    stored = SimpleNamespace(id=900, session_id=500, content="fixture", created_at=datetime.now(UTC))
    patch_main_dependency(monkeypatch, module, "require_owned_chat_request", AsyncMock(return_value=(message, stored)))
    monkeypatch.setattr(module.conversation_context, "load_history", AsyncMock(return_value=([], None, {})))
    patch_main_dependency(monkeypatch, module, "load_prompt_contents", AsyncMock(return_value=[]))
    # 隔离授权桩不访问数据库；真实持久游标由订阅/进程恢复集成测试覆盖。
    patch_main_dependency(monkeypatch, module, "next_chat_event_sequence", AsyncMock(return_value=1))
    patch_main_dependency(monkeypatch, module, "audited_sse_message", audited)
    patch_main_dependency(monkeypatch, module, "finalize_chat_response", finalize)
    patch_main_dependency(monkeypatch, module, "stream_chat_completion", deny_real_io)
    # 本用例验证 SSE 游标；持久化 trace 的事务由真实数据库集成测试覆盖。
    trace_store = SimpleNamespace(
        create_run=AsyncMock(return_value=SimpleNamespace(run_id="fixture-run", trace_id="fixture-trace")),
        append_graph_events=AsyncMock(),
        append_checkpoint=AsyncMock(),
    )
    monkeypatch.setattr(module.agent_runtime, "runtime_store", trace_store)

    async def collect():
        return [chunk async for chunk in module._sse_events_inner("empty-state", principal(module))]

    chunks = asyncio.run(collect())
    expected = ["processmsg", "processmsg", "aimsg", "generationerror", "done"] if message_state == "unconfigured" else [
        "processmsg", "aimsg", "done",
    ]
    assert [event for _, event in recorded] == expected
    assert [sequence for sequence, _ in recorded] == list(range(1, len(expected) + 1))
    assert "event: done" in chunks[-1]
    if message_state == "unconfigured":
        assert '"status": "failed"' in chunks[-1]


def test_local_upload_routes_through_shared_upload_service(main_module, monkeypatch, tmp_path):
    from io import BytesIO
    from fastapi import UploadFile

    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "UPLOAD_ROOT", tmp_path)
    service = AsyncMock(return_value=[{"id": 700, "accessMode": "private", "size": 7}])
    patch_main_dependency(monkeypatch, module, "save_uploads", service)
    session = PolicySession()
    result = asyncio.run(module.file_upload(UploadFile(filename="fixture.txt", file=BytesIO(b"fixture")), principal(module), session))
    assert result["accessMode"] == "private" and "path" not in result
    assert service.await_count == 1 and session.commits == 0
    assert service.call_args.args[3] == tmp_path


def test_shared_file_import_commits_before_queue(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "new_id", AsyncMock(return_value=800))
    base = SimpleNamespace(id=600, tenant_id=1, name="fixture", resource_type="knowledge_bases", is_deleted=False, data={})
    session = PolicySession({("Resource", 600): base, ("Resource", 700): policy_file(mode="tenant", owner=11),
                             ("KnowledgeBase", 600): SimpleNamespace(id=600, tenant_id=1)})
    queued = []

    async def queue(document_id, tenant_id, background_tasks):
        assert session.commits == 1
        queued.append((document_id, tenant_id))
        return "fixture-job", "queued"

    patch_route_dependency(monkeypatch, module.knowledge_import_file, "new_id", AsyncMock(return_value=800))
    patch_route_dependency(monkeypatch, module.knowledge_import_file, "queue_or_run_document", queue)
    result = asyncio.run(module.knowledge_import_file(600, 700, BackgroundTasks(), principal(module), session))
    assert result["documentId"] == 800 and base.data["fileIds"] == [700] and queued == [(800, 1)]
    assert session.get_calls[1][2] == {"with_for_update": True, "populate_existing": True}


def test_shared_file_replacement_commits_before_queue(main_module, monkeypatch):
    module = main_module
    patch_main_dependency(monkeypatch, module, "has_permission", AsyncMock(return_value=True))
    endpoint = next(route.endpoint for route in module.app.routes if getattr(route, "path", "") == "/api/AIKmss/ReplaceDocumentFile")
    patch_route_dependency(monkeypatch, endpoint, "new_id", AsyncMock(return_value=900))
    document = SimpleNamespace(id=800, tenant_id=1, knowledge_base_id=600, file_id=701, source_name="old", is_deleted=False, source_revision=0)
    session = PolicySession({
        ("KnowledgeDocument", 800): document, ("Resource", 700): policy_file(mode="tenant"),
        ("Resource", 600): SimpleNamespace(id=600, tenant_id=1, name="fixture", resource_type="knowledge_bases", is_deleted=False),
        ("KnowledgeBase", 600): SimpleNamespace(id=600, tenant_id=1),
    })

    async def queue(document_id, tenant_id, background_tasks):
        assert session.commits == 1 and document.file_id == 700
        return "fixture-job", "queued"

    queue_mock = AsyncMock(side_effect=queue)
    patch_route_dependency(monkeypatch, endpoint, "queue_or_run_document", queue_mock)
    result = asyncio.run(endpoint(800, 700, BackgroundTasks(), principal(module), session))
    assert result["fileId"] == 700 and document.content == "" and session.commits == 1
    queue_mock.assert_awaited_once_with(800, 1, ANY)


@pytest.mark.parametrize("fail_after_save", [False, True])
def test_kb_resource_and_document_have_one_commit(main_module, monkeypatch, fail_after_save):
    module = main_module
    patch_main_dependency(monkeypatch, module, "ensure_permission", AsyncMock())
    patch_main_dependency(monkeypatch, module, "attach_operation_logs", AsyncMock())
    patch_main_dependency(monkeypatch, module, "new_id", AsyncMock(return_value=800))
    base = SimpleNamespace(id=600, tenant_id=1, name="fixture", chunk_size=1200, chunk_overlap=0,
                           retrieval_strategy="basic", retrieval_config={}, embedding_model_id=None, rerank_model_id=None)
    session = PolicySession({("KnowledgeBase", 600): base, ("Resource", 700): policy_file(mode="tenant")})
    queue = AsyncMock()
    patch_route_dependency(monkeypatch, module.knowledge_base_add_edit, "new_id", AsyncMock(return_value=800))
    patch_route_dependency(monkeypatch, module.knowledge_base_add_edit, "queue_or_run_document", queue)
    if fail_after_save:
        patch_main_dependency(monkeypatch, module, "apply_knowledge_payload", lambda *args: (_ for _ in ()).throw(ValueError("fixture-failure")))
        with pytest.raises(ValueError, match="fixture-failure"):
            asyncio.run(module.knowledge_base_add_edit({"id": 600, "fileIds": [700]}, BackgroundTasks(), principal(module), session))
        assert session.commits == 0 and queue.call_count == 0
    else:
        result = asyncio.run(module.knowledge_base_add_edit({
            "id": 600, "name": "fixture", "aIKmssDetailsList": [{"fileId": 700}],
        }, BackgroundTasks(), principal(module), session))
        assert result is True and session.commits == 1
        assert session.records[("KnowledgeDocument", 800)].file_id == 700
        queue.assert_awaited_once_with(800, 1, ANY)


def test_worker_embedding_denies_private_file_before_provider(main_module, monkeypatch):
    from eic import workers

    session = PolicySession({
        ("KnowledgeDocument", 800): SimpleNamespace(id=800, tenant_id=1, knowledge_base_id=600, file_id=700),
        ("KnowledgeBase", 600): SimpleNamespace(id=600, tenant_id=1),
        ("Resource", 700): policy_file(),
    })

    class Context:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(workers, "SessionLocal", Context)
    provider = AsyncMock()
    monkeypatch.setattr(workers, "embed_texts", provider)
    result = asyncio.run(workers.process_document_embeddings({}, 800, 1))
    assert result["status"] == "file_unavailable" and provider.call_count == 0 and session.commits == 0


@pytest.mark.parametrize("mode,expected_count", [("private", 0), ("tenant", 1), ("legacy", 1)])
def test_retrieval_filters_private_and_rebuilds_dense_payload(main_module, monkeypatch, mode, expected_count):
    from eic.knowledge import ops
    from eic.knowledge.indexing import parameter_snapshot
    from eic.vector.qdrant_reader import VectorHit

    chunk = SimpleNamespace(id=900, tenant_id=1, knowledge_base_id=600, document_id=800,
                            parent_chunk_id=None, chunk_index=0, index_version=1001, content="trusted-local-content", attributes={"chunk_kind": "child"})
    document = SimpleNamespace(id=800, tenant_id=1, knowledge_base_id=600, file_id=700, source_name="fixture", active_index_version=1001)
    model = main_module.AIModel(id=1000, tenant_id=1, provider="openai", enabled=True, model_type="embedding",
                               endpoint="http://fixture.invalid", model_name="fixture", embedding_dimensions=1)

    class RetrievalSession(PolicySession):
        async def scalars(self, statement):
            entity = statement.column_descriptions[0]["entity"]
            if entity is main_module.KnowledgeDocument:
                return [document]
            if entity is main_module.Resource:
                return [self.records[("Resource", 700)]]
            if entity is main_module.KnowledgeChunk:
                return [chunk]
            return [ingestion]

    session = RetrievalSession({("Resource", 700): policy_file(mode=mode)})
    base = main_module.KnowledgeBase(id=600, tenant_id=1, name="fixture", chunk_size=1200, chunk_overlap=0,
                                     retrieval_strategy="basic", retrieval_config={}, embedding_model_id=1000, rerank_model_id=None)
    ingestion = SimpleNamespace(id=1001, status="ready", parameters=parameter_snapshot(base, model))
    session.records[("KnowledgeBase", 600)] = base
    session.records[("AIModel", 1000)] = model
    dense = AsyncMock(return_value=([
        VectorHit("900", 0.9, {"content": "forged-external-content", "documentId": 999}),
        VectorHit("901", 0.8, {"content": "unauthorized-content"}),
    ], "ok"))
    monkeypatch.setattr(ops, "_search_dense", dense)
    monkeypatch.setattr(ops, "SessionLocal", lambda: nullcontext(session))
    result = asyncio.run(ops.retrieve_knowledge(session, tenant_id=1, base=base, query="fixture"))
    assert result["denseCount"] == expected_count and len(result["results"]) == expected_count
    assert "forged-external-content" not in str(result) and "unauthorized-content" not in str(result)
    if expected_count:
        assert result["results"][0]["content"] == "trusted-local-content"
        assert result["results"][0]["documentId"] == 800

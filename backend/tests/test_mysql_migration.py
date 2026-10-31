import asyncio
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eic.core.security import decrypt_secret
from eic.models import (
    AIApp,
    AIModel,
    Base,
    Department,
    KnowledgeBase,
    LegacyRecord,
    Resource,
    Role,
    Tenant,
    User,
    UserRole,
)


def load_migrator():
    path = Path(__file__).parents[2] / "migration" / "scripts" / "migrate_mysql.py"
    spec = importlib.util.spec_from_file_location("eic_mysql_migrator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mysql_import_preserves_ids_maps_tenant_codes_and_redacts_secrets(tmp_path: Path) -> None:
    async def run() -> tuple[dict, list[object], list[object], list[object], list[object], list[object]]:
        source_engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}")
        target_engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}")
        try:
            async with source_engine.begin() as connection:
                await connection.execute(text("CREATE TABLE TTenant (Id INTEGER, Code INTEGER, Name TEXT, Status INTEGER, IsDelete INTEGER, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TUser (Id INTEGER, TenantId INTEGER, Name TEXT, NickName TEXT, PasswordHash TEXT, Status INTEGER, IsSuperAdmin INTEGER, IsDelete INTEGER, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TRole (Id INTEGER, TenantId INTEGER, Name TEXT, Remarks TEXT, IsDelete INTEGER)"))
                await connection.execute(text("CREATE TABLE TPermission (Id TEXT, FullName TEXT, ModuleName TEXT)"))
                await connection.execute(text("CREATE TABLE TUserBindRole (UserId INTEGER, RoleId INTEGER)"))
                await connection.execute(text("CREATE TABLE TAIModels (Id INTEGER, TenantId INTEGER, AIType TEXT, AIModelType TEXT, EndPoint TEXT, ModelName TEXT, ModelKey TEXT, EmbeddingValueSize INTEGER, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TAIPrompts (Id INTEGER, TenantId INTEGER, Name TEXT, Prompt TEXT, Description TEXT, IsDelete INTEGER, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TAIApps (Id INTEGER, TenantId INTEGER, Name TEXT, Describe TEXT, ChatModelID TEXT, KmsId INTEGER, AIPromptID INTEGER, SecretKey TEXT, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TAIKmss (Id INTEGER, TenantId INTEGER, Name TEXT, aIModelsId INTEGER, MaxTokensPerParagraph INTEGER, MaxTokensPerLine INTEGER, OverlappingTokens INTEGER, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TFile (Id INTEGER, TenantId INTEGER, Name TEXT, Path TEXT, Url TEXT, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TAIChats (Id INTEGER, TenantId INTEGER, UserId INTEGER, AppId INTEGER, Name TEXT, LastMessage TEXT, CreateTime TEXT)"))
                await connection.execute(text("CREATE TABLE TAIChatHistorys (Id INTEGER, TenantId INTEGER, AIChatsId INTEGER, Content TEXT, IsSend INTEGER, InputTokenCount INTEGER, OutputTokenCount INTEGER, CreateTime TEXT)"))
                await connection.execute(text("INSERT INTO TTenant VALUES (100, 1000, '演示租户', 1, 0, '2026-01-01T00:00:00+00:00')"))
                password_hash = hashlib.sha256(b"legacy-password").hexdigest()
                await connection.execute(text("INSERT INTO TUser VALUES (200, 1000, 'legacy-user', '旧用户', :password_hash, 1, 0, 0, '2026-01-01T00:00:00+00:00')"), {"password_hash": password_hash})
                await connection.execute(text("INSERT INTO TRole VALUES (300, 1000, '读者', '只读', 0)"))
                await connection.execute(text("INSERT INTO TPermission VALUES ('ai.read', 'AI Read', 'AI')"))
                await connection.execute(text("INSERT INTO TUserBindRole VALUES (200, 300)"))
                await connection.execute(text("INSERT INTO TAIModels VALUES (400, 1000, 'openai', 'chat', 'https://model.example/v1', 'demo', 'model-secret', 1024, '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TAIPrompts VALUES (500, 1000, '提示词', '回答问题', '说明', 0, '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TAIApps VALUES (600, 1000, '应用', '描述', '400', NULL, 500, 'app-secret', '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TAIKmss VALUES (700, 1000, '知识库', 400, 299, 99, 49, '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TFile VALUES (800, 1000, '说明.txt', 'C:/old/说明.txt', '', '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TAIChats VALUES (900, 1000, 200, 600, '会话', '你好', '2026-01-01T00:00:00+00:00')"))
                await connection.execute(text("INSERT INTO TAIChatHistorys VALUES (1000, 1000, 900, '你好', 1, 2, 0, '2026-01-01T00:00:00+00:00')"))
            async with target_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            migrator = load_migrator()
            report = await migrator.run(SimpleNamespace(
                source_url=f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}",
                target_url=f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}",
                dry_run=False, prepare_schema=False, allow_missing_tables=True,
            ))
            maker = async_sessionmaker(target_engine, expire_on_commit=False, class_=AsyncSession)
            async with maker() as session:
                tenant = await session.get(Tenant, 100)
                user = await session.get(User, 200)
                role = await session.get(Role, 300)
                model = await session.get(AIModel, 400)
                resources = list(await session.scalars(select(Resource).where(Resource.id.in_([600, 700, 800]))))
                archive = await session.scalar(select(LegacyRecord).where(LegacyRecord.source_table == "TAIModels"))
                bindings = list(await session.scalars(select(UserRole).where(UserRole.user_id == 200)))
            return report, [tenant], [user], [role], [model], [resources, archive, bindings]
        finally:
            await source_engine.dispose()
            await target_engine.dispose()

    report, tenants, users, roles, models, extras = asyncio.run(run())
    tenant, user, role, model = tenants[0], users[0], roles[0], models[0]
    resources, archive, bindings = extras
    assert report["status"] == "partial"
    assert report["source_errors"]
    assert report["tables"]["TUser"]["target_verified"] == 1
    assert tenant and tenant.id == 100 and tenant.code == 1000
    assert user and user.id == 200 and user.tenant_id == 100
    assert role and role.id == 300 and model and model.id == 400
    assert bindings and {item.role_id for item in bindings} == {300}
    assert {item.id for item in resources} == {600, 700, 800}
    assert model.secret_encrypted is None
    assert archive and "ModelKey" not in archive.payload and "model-secret" not in str(archive.payload)
    assert all("SecretKey" not in str(item.data) and "app-secret" not in str(item.data) for item in resources)
    app_resource = next(item for item in resources if item.id == 600)
    assert decrypt_secret(app_resource.data["secret_encrypted"]) == "app-secret"
    assert report["application_secrets_encrypted"] == 1


def test_mysql_import_blocks_collisions_preserves_references_and_refuses_unproven_rerun(tmp_path: Path) -> None:
    async def run() -> tuple[dict, dict, list[Resource], list[Department], list[LegacyRecord]]:
        target_engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'target.db').as_posix()}")
        try:
            async with target_engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            migrator = load_migrator()
            tables = {
                "TTenant": [{"Id": 1, "Code": 10, "Name": "Tenant", "Status": 1}],
                "TUser": [{"Id": 2, "TenantId": 10, "Name": "user", "PasswordHash": hashlib.sha256(b"pw").hexdigest()}],
                "TAIModels": [{"Id": 3, "TenantId": 10, "AIType": "ollama", "AIModelType": "chat", "EndPoint": "http://model", "ModelName": "chat"}],
                "TAIPrompts": [{"Id": 4, "TenantId": 10, "Name": "prompt", "Prompt": "answer"}],
                "TAIApps": [{"Id": 4, "TenantId": 10, "Name": "app", "ChatModelID": 999, "AIPromptID": 999, "KmsId": 999, "api_key": "secret-api", "Nested": {"Authorization": "Bearer secret", "api_key": "nested-secret"}}],
                "TAIKmss": [{"Id": 5, "TenantId": 10, "Name": "kb", "aIModelsId": 999}],
                "TDepartment": [
                    {"Id": 20, "TenantId": 10, "Name": "root", "ParentId": None},
                    {"Id": 21, "TenantId": 10, "Name": "child", "ParentId": 20},
                    {"Id": 22, "TenantId": 10, "Name": "orphan", "ParentId": 999},
                ],
                "TPermission": [],
                "TRole": [],
                "TUserBindRole": [],
                "TRolePermission": [],
            }
            source_errors: dict[str, str] = {}
            maker = async_sessionmaker(target_engine, expire_on_commit=False, class_=AsyncSession)
            async with maker() as session:
                with pytest.raises(migrator.MigrationPreflightError,match='source_resource_id_collision'):
                    await migrator.import_rows(session, tables, source_errors, dry_run=False)
                await session.rollback()
                assert list(await session.scalars(select(Tenant)))==[]
                assert list(await session.scalars(select(Resource)))==[]
            tables['TAIApps'][0]['Id']=6
            async with maker() as session:
                first = await migrator.import_rows(session, tables, source_errors, dry_run=False)
            async with maker() as session:
                with pytest.raises(migrator.MigrationPreflightError,match='target_entity_id_collision'):
                    await migrator.import_rows(session, tables, source_errors, dry_run=False)
                await session.rollback()
            async with maker() as session:
                resources = list(await session.scalars(select(Resource).where(Resource.legacy_source_id.in_([4, 5, 6]))))
                departments = list(await session.scalars(select(Department).order_by(Department.id)))
                archive = list(await session.scalars(select(LegacyRecord).where(LegacyRecord.source_table == "TAIApps")))
                apps = list(await session.scalars(select(AIApp)))
                knowledge_bases = list(await session.scalars(select(KnowledgeBase)))
            assert len(resources) == 3
            assert len(archive) == 1
            assert len(apps) == 1 and apps[0].chat_model_id is None and apps[0].prompt_id is None and apps[0].knowledge_base_id is None
            assert len(knowledge_bases) == 1 and knowledge_bases[0].embedding_model_id is None
            assert first["warnings"]
            return first, resources, departments, archive
        finally:
            await target_engine.dispose()

    first, resources, departments, archive = asyncio.run(run())
    by_id = {item.id: item for item in departments}
    assert by_id[21].parent_id == 20
    assert by_id[22].parent_id is None
    assert len({item.id for item in resources}) == 3
    assert archive and "Authorization" not in str(archive[0].payload)
    assert "secret" not in str(archive[0].payload)
    assert "api_key" not in str(archive[0].payload)
    assert first["tables"]["TAIApps"]["migrated"] == 1

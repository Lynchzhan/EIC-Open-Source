"""共享源身份、实体/关系核验及只读预览；不会删除源数据或自动修复历史关系。"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import re
from pathlib import Path
import sys
from typing import Any
from urllib.parse import quote

from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from eic.core.security import decrypt_secret
from eic.models import (
    AgentMemory, AIApp, AIModel, AIPrompt, Base, ChatMessage, ChatSession,
    Department, KnowledgeBase, KnowledgeDocument, LegacyRecord, Permission,
    Position, Resource, Role, RolePermission, Tenant, User, UserRole,
)

ENTITY_MAP = {
    "TTenant": Tenant, "TUser": User, "TRole": Role, "TPermission": Permission,
    "TDepartment": Department, "TPosition": Position, "TAIModels": AIModel,
    "TAIPrompts": AIPrompt, "TAIApps": AIApp, "TAIKmss": KnowledgeBase,
    "TAIKmsDetails": KnowledgeDocument, "TAIChats": ChatSession,
    "TAIChatHistorys": ChatMessage, "TAIAgentMemory": AgentMemory,
}
RESOURCE_MAP = {
    "TAIPrompts": "ai_prompts", "TAIApps": "ai_apps", "TAIKmss": "knowledge_bases",
    "TFile": "files", "TAIChats": "chats",
}
BINDING_MAP = {"TUserBindRole": UserRole, "TRolePermission": RolePermission}
SNAPSHOT_MODELS = tuple(dict.fromkeys([*ENTITY_MAP.values(), Resource, LegacyRecord, UserRole, RolePermission]))


class MigrationPreflightError(ValueError):
    """仅携带固定安全码，在迁移写入前拒绝不明确的身份。"""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def positive_id(raw):
    if type(raw) is int:
        number = raw
    elif isinstance(raw, str) and re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        number = int(raw)
    else:
        raise MigrationPreflightError("invalid_source_identity")
    if not 0 < number < 2**63:
        raise MigrationPreflightError("invalid_source_identity")
    return number


def source_tenant_codes(tables):
    """TenantId 的唯一输入语义是 code；禁止主键回退及重复映射。"""
    by_code, seen_ids = {}, set()
    for row in tables.get("TTenant", []):
        source_id = positive_id(value(row, "Id"))
        code = positive_id(value(row, "Code"))
        if source_id in seen_ids or code in by_code:
            raise MigrationPreflightError("ambiguous_source_tenant")
        seen_ids.add(source_id)
        by_code[code] = source_id
    for rows in tables.values():
        for row in rows:
            # 同时出现不同别名也不能根据字典顺序选择租户。
            aliases = [raw for key, raw in row.items()
                       if str(key).lower() in {"tenantid", "tenant_id"}]
            codes = [positive_id(raw) for raw in aliases if raw not in (None, "")]
            if len(set(codes)) > 1 or (codes and len(codes) != len(aliases)):
                raise MigrationPreflightError("ambiguous_source_tenant")
            if codes and codes[0] not in by_code:
                raise MigrationPreflightError("unknown_source_tenant_code")
    return by_code


def source_resource_scopes(tables):
    by_code = source_tenant_codes(tables)
    scopes = {}
    for table, resource_type in RESOURCE_MAP.items():
        for row in tables.get(table, []):
            source_id = positive_id(value(row, "Id"))
            code = positive_id(value(row, "TenantId", "tenant_id"))
            if code not in by_code:
                raise MigrationPreflightError("unknown_source_tenant_code")
            if source_id in scopes:
                raise MigrationPreflightError("source_resource_id_collision")
            scopes[source_id] = (table, resource_type, by_code[code])
    return scopes


async def preflight_target(session, tables):
    """只读预检；调用方必须在归档、实体赋值和 flush 之前调用。"""
    scopes = source_resource_scopes(tables)
    with session.no_autoflush:
        # 旧版已经重编号的资源即使只被本批引用，也不能继续假装同 ID 契约成立。
        remapped = await session.scalar(select(Resource.id).where(
            Resource.resource_type.in_(set(RESOURCE_MAP.values())),
            Resource.legacy_source_id.is_not(None),
            Resource.id != Resource.legacy_source_id,
        ).limit(1))
        if remapped is not None:
            raise MigrationPreflightError("target_resource_identity_remapped")
        for row in tables.get("TTenant", []):
            source_id, code = positive_id(value(row, "Id")), positive_id(value(row, "Code"))
            existing = await session.get(Tenant, source_id)
            same_code = await session.scalar(select(Tenant).where(Tenant.code == code))
            if ((existing is not None and existing.code != code)
                    or (same_code is not None and same_code.id != source_id)):
                raise MigrationPreflightError("target_tenant_identity_conflict")
        # 数字 ID 与租户相同也不能证明同源，禁止覆盖任意既存实体。
        # 需要重跑/合并时应先形成明确身份映射，不能据 LegacyRecord 推断写入授权。
        for table, model in ENTITY_MAP.items():
            seen_ids = set()
            for row in tables.get(table, []):
                raw = value(row, "Id")
                if raw in (None, ""):
                    continue
                source_id = str(raw).strip() if model is Permission else positive_id(raw)
                if source_id in seen_ids:
                    raise MigrationPreflightError("duplicate_source_entity_id")
                seen_ids.add(source_id)
                if await session.get(model, source_id) is not None:
                    raise MigrationPreflightError("target_entity_id_collision")
        for source_id, (table, kind, tenant_id) in scopes.items():
            existing = await session.get(Resource, source_id)
            if existing is not None:
                raise MigrationPreflightError("target_resource_id_collision")
            aliases = list(await session.scalars(select(Resource).where(
                Resource.resource_type == kind, Resource.legacy_source_id == source_id)))
            if any(item.id != source_id or item.tenant_id != tenant_id for item in aliases):
                raise MigrationPreflightError("target_resource_identity_remapped")
            model = ENTITY_MAP.get(table)
            dedicated = await session.get(model, source_id) if model else None
            if dedicated is not None and dedicated.tenant_id != tenant_id:
                raise MigrationPreflightError("target_entity_tenant_conflict")


def database_identity(url):
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if not parsed.database or parsed.database == ":memory:" or parsed.database.startswith("file:"):
            raise ValueError("Use a regular SQLite file")
        path = str(Path(parsed.database).resolve())
        return ("sqlite", path.casefold() if sys.platform == "win32" else path)
    return (parsed.get_backend_name(), parsed.host, parsed.port, parsed.database)


def value(row, *names, default=None):
    fields = {str(key).lower(): item for key, item in row.items()}
    return next((fields[name.lower()] for name in names if name.lower() in fields), default)


def integer(raw):
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return None


def source_identity(table, row, payload):
    raw = value(row, "Id")
    if raw is not None and str(raw).strip():
        return str(raw)
    components = {
        "TUserBindRole": ("UserId", "RoleId"),
        "TRolePermission": ("RoleId", "PermissionId"),
        "TUserBindPosition": ("UserId", "PositionId"),
    }.get(table)
    if components and all(value(row, key) is not None for key in components):
        return ":".join(str(value(row, key)) for key in components)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()[:32]


async def read_only_engine(url):
    """SQLite 在连接前启用 mode=ro，避免预览创建不存在的文件。"""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if not parsed.database or parsed.database == ":memory:" or parsed.database.startswith("file:"):
            raise ValueError("Use an existing SQLite file for the read-only target")
        path = Path(parsed.database).resolve(strict=True)
        uri = "file:" + quote(path.as_posix(), safe="/:")
        parsed = parsed.set(database=uri, query={"mode": "ro", "uri": "true"})
    return create_async_engine(parsed)


async def snapshot_target(url):
    engine = await read_only_engine(url)
    result = {}
    try:
        async with engine.connect() as connection:
            if connection.dialect.name in {"postgresql", "mysql"}:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            names = set(await connection.run_sync(lambda c: inspect(c).get_table_names()))
            missing = [model.__tablename__ for model in SNAPSHOT_MODELS if model.__tablename__ not in names]
            if missing:
                raise ValueError("Target migration schema is incomplete")
            for model in SNAPSHOT_MODELS:
                rows = (await connection.execute(select(model.__table__))).mappings().all()
                result[model.__tablename__] = [dict(row) for row in rows]
            await connection.rollback()
    finally:
        await engine.dispose()
    return result


async def preview_engine(target_url):
    snapshot = await snapshot_target(target_url)
    # 所有预览DML仅作用于这个内存副本，不是目标连接的事务回滚伪dry-run。
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            for model in SNAPSHOT_MODELS:
                rows = snapshot[model.__tablename__]
                if rows:
                    await connection.execute(model.__table__.insert(), rows)
    except BaseException:
        await engine.dispose()
        raise
    return engine


async def verify_integrity(session, tables, source_errors, payload_for_row):
    indexes = {}
    for model in ENTITY_MAP.values():
        indexes[model] = {item.id: item for item in await session.scalars(select(model))}
    resources = list(await session.scalars(select(Resource)))
    resource_index = {(item.resource_type, item.legacy_source_id): item for item in resources}
    archives = list(await session.scalars(select(LegacyRecord)))
    archive_index = {(item.source_table, item.source_id): item for item in archives}
    user_roles = {(item.user_id, item.role_id) for item in await session.scalars(select(UserRole))}
    role_permissions = {(item.role_id, item.permission_id) for item in await session.scalars(select(RolePermission))}
    by_code = source_tenant_codes(tables)
    issues = []
    fatal = []
    relations = []
    counts = {}
    source_entities = {}

    def tenant(row):
        raw = integer(value(row, "TenantId", "tenant_id"))
        return by_code.get(raw)

    def issue(kind, table, identity, field=None):
        entry = {"kind": kind, "table": table, "source_id": str(identity)}
        if field:
            entry["field"] = field
        issues.append(entry)

    for table, rows in tables.items():
        identities = [source_identity(table, row, payload_for_row(row)) for row in rows]
        duplicates = sum(number - 1 for number in Counter(identities).values() if number > 1)
        archived = verified = 0
        mode = "entity" if table in ENTITY_MAP else "resource" if table == "TFile" else "relation" if table in BINDING_MAP else "archive_only"
        for row, identity in zip(rows, identities):
            archive = archive_index.get((table, identity))
            if archive is not None:
                archived += 1
                if archive.payload != payload_for_row(row):
                    fatal.append({"kind": "archive_payload_mismatch", "table": table, "source_id": identity})
            else:
                issue("missing_archive", table, identity)
            if table in ENTITY_MAP:
                raw = value(row, "Id")
                key = str(raw).strip() if table == "TPermission" and raw is not None else integer(raw)
                item = indexes[ENTITY_MAP[table]].get(key)
                expected_tenant = tenant(row)
                present = item is not None and (table in {"TTenant", "TPermission"} or
                                               expected_tenant is not None and item.tenant_id == expected_tenant)
                if present:
                    source_entities[(table, identity)] = item
                if table in RESOURCE_MAP:
                    resource = resource_index.get((RESOURCE_MAP[table], integer(raw)))
                    present = (present and resource is not None and resource.id == key
                               and resource.tenant_id == expected_tenant)
            elif table == "TFile":
                item = resource_index.get(("files", integer(value(row, "Id"))))
                present = (item is not None and item.id == integer(value(row, "Id"))
                           and tenant(row) is not None and item.tenant_id == tenant(row))
            elif table == "TUserBindRole":
                user = indexes[User].get(integer(value(row, "UserId")))
                role = indexes[Role].get(integer(value(row, "RoleId")))
                present = user is not None and role is not None and user.tenant_id == role.tenant_id and (user.id, role.id) in user_roles
            elif table == "TRolePermission":
                role_id = integer(value(row, "RoleId"))
                permission_id = str(value(row, "PermissionId", default=""))
                present = role_id in indexes[Role] and permission_id in indexes[Permission] and (role_id, permission_id) in role_permissions
            else:
                present = archive is not None
            if present:
                verified += 1
            elif mode != "archive_only":
                issue("missing_or_invalid_target", table, identity)
        counts[table] = {"source": len(rows), "distinct_source_identities": len(set(identities)),
                         "duplicates": duplicates, "archived": archived,
                         "target_verified": verified, "mapping": mode}
        if duplicates:
            issue("duplicate_source_identity", table, "multiple")
    for table in source_errors:
        counts.setdefault(table, {"source": None, "archived": 0, "target_verified": 0, "mapping": "unavailable"})

    def relation(table, row, identity, field, raw, model, actual):
        if raw is None or raw == "":
            return
        key = integer(raw)
        expected = indexes[model].get(key)
        valid = expected is not None and expected.tenant_id == tenant(row) and actual == key
        relations.append({"table": table, "source_id": identity, "field": field,
                          "expected_id": str(raw), "actual_id": str(actual) if actual is not None else None,
                          "verified": bool(valid)})
        if not valid:
            issue("broken_relation", table, identity, field)

    for table, rows in tables.items():
        for row in rows:
            identity = source_identity(table, row, payload_for_row(row))
            item = source_entities.get((table, identity))
            if item is None:
                continue
            specs = {
                "TAIApps": [("ChatModelID", AIModel, "chat_model_id"), ("AIPromptID", AIPrompt, "prompt_id"),
                            ("KmsId", KnowledgeBase, "knowledge_base_id")],
                "TAIKmss": [("aIModelsId", AIModel, "embedding_model_id"), ("AIRerankModelsId", AIModel, "rerank_model_id")],
                "TAIKmsDetails": [("KmsId", KnowledgeBase, "knowledge_base_id")],
                "TAIChats": [("UserId", User, "owner_id"), ("AppId", AIApp, "app_id")],
                "TAIChatHistorys": [("AIChatsId", ChatSession, "session_id")],
                "TAIAgentMemory": [("UserId", User, "owner_id"), ("AIAppsId", AIApp, "agent_id")],
                "TDepartment": [("ParentId", Department, "parent_id")],
            }.get(table, [])
            for field, model, attribute in specs:
                raw = value(row, field, "AIModelsId" if field == "aIModelsId" else field)
                relation(table, row, identity, field, raw, model, getattr(item, attribute))
            if table == "TAIApps":
                config = item.config or {}
                relation(table, row, identity, "RerankModelID", value(row, "RerankModelID"),
                         AIModel, config.get("rerankModelId"))
                resource = resource_index.get(("ai_apps", integer(value(row, "Id"))))
                for key, attribute in [("chatModelId", "chat_model_id"), ("promptId", "prompt_id"), ("knowledgeBaseId", "knowledge_base_id")]:
                    if config.get(key) != getattr(item, attribute) or resource is None or (resource.data or {}).get(key) != getattr(item, attribute):
                        issue("resource_relation_mismatch", table, identity, key)
                secret = value(row, "SecretKey")
                if secret not in (None, ""):
                    try:
                        valid = decrypt_secret(config.get("secret_encrypted") or "") == str(secret)
                        valid = valid and resource is not None and decrypt_secret((resource.data or {}).get("secret_encrypted") or "") == str(secret)
                    except Exception:
                        valid = False
                    if not valid:
                        fatal.append({"kind": "application_secret_verification_failed", "table": table, "source_id": identity})
            if table == "TAIKmsDetails" and value(row, "FileId") not in (None, ""):
                file = resource_index.get(("files", integer(value(row, "FileId"))))
                valid = file is not None and file.tenant_id == tenant(row) and item.file_id == file.id
                relations.append({"table": table, "source_id": identity, "field": "FileId", "verified": valid})
                if not valid:
                    issue("broken_relation", table, identity, "FileId")
    # 未知历史环不自动断边；触及环时失败回滚而非留下新的可见环。
    departments = indexes[Department]
    for row in tables.get("TDepartment", []):
        node = integer(value(row, "Id"))
        visited = set()
        while node in departments:
            if node in visited:
                fatal.append({"kind": "department_cycle", "table": "TDepartment", "source_id": str(node)})
                break
            visited.add(node)
            node = departments[node].parent_id
    return {
        "tables": counts, "relations": relations, "issues": issues, "fatal_errors": fatal,
        "source_error_count": len(source_errors), "relation_count": len(relations),
        "verified_relation_count": sum(bool(item["verified"]) for item in relations),
        "complete": not source_errors and not issues and not fatal,
        "secret_policy": {"model_keys": "discard_and_reconfigure", "application_secrets": "encrypted_and_decryption_verified",
                          "password_hashes": "active_user_only_not_archived_or_reported"},
        "scope": "documented entities and relations plus explicit archive_only mappings, not file-copy/index/product readiness",
    }

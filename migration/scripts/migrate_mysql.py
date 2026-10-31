"""Import legacy MySQL rows into the Enterprise Intelligence Center schema.

The importer is deliberately explicit and refuses ambiguous target identities:

* source tables are a fixed allowlist, never user-supplied SQL;
* source IDs are preserved; existing target entity IDs are never overwritten;
* legacy tenant codes are resolved to the target tenant primary keys;
* unsupported fields are retained in ``legacy_records`` after redaction;
* ModelKey, application secrets, and source password hashes are never copied
  to the archive payload or migration reports;
* the source is read-only and is never updated or deleted.

Re-importing a committed batch is rejected on ID collisions. Merge/resume requires
an explicit identity mapping and a separate reviewed workflow.

Run Alembic against the target first. ``--prepare-schema`` exists only for
isolated empty-database demonstrations and tests.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_SRC = Path(__file__).parents[2] / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
SCRIPT_DIR = Path(__file__).parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from migration_integrity import (
    MigrationPreflightError, database_identity, preflight_target,
    preview_engine, read_only_engine, source_identity, source_resource_scopes, verify_integrity,
)

from eic.core.security import encrypt_secret, hash_password
from eic.models import (
    AgentMemory,
    AIApp,
    AIModel,
    AIPrompt,
    Base,
    ChatMessage,
    ChatSession,
    Department,
    KnowledgeBase,
    KnowledgeDocument,
    LegacyRecord,
    Permission,
    Position,
    Resource,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)

SOURCE_TABLES = (
    "TTenant", "TUser", "TUserInfo", "TRole", "TPermission", "TRolePermission",
    "TUserBindRole", "TUserBindPosition", "TDepartment", "TPosition", "TFile",
    "TAIModels", "TAIApps", "TAIAppsBindId", "TAIPrompts", "TAIChats",
    "TAIChatHistorys", "TAIKmss", "TAIKmsDetails", "TAIAgentMemory",
    "TAISkillToolManagement", "TAIChatHistorysBindLog", "TMessage", "TMessageRead",
    "THttpLog", "TOSLog", "TSign", "TDictionary", "TWeiXinKey", "TRegionProvince",
    "TRegionCity", "TRegionArea", "TRegionTown",
)

SENSITIVE_KEY = re.compile(
    r"(?:modelkey|secretkey|apikey|password(?:hash)?|token|credential|authorization|mcpheaders|mcpcommand|mcparguments|mcpenvironment)$",
    re.IGNORECASE,
)

class MigrationIntegrityError(RuntimeError):
    def __init__(self, integrity):
        super().__init__("Migration integrity verification failed")
        self.integrity = integrity


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    netloc = parsed.hostname or ""
    if ":" in netloc and not netloc.startswith("["):
        netloc = "[" + netloc + "]"
    if parsed.port:
        netloc += f":{parsed.port}"
    if parsed.username is not None or parsed.password is not None:
        netloc = "REDACTED@" + netloc
    query = "&".join(quote(part.split("=", 1)[0], safe="") + "=REDACTED"
                     for part in parsed.query.split("&") if part)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))


def json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return {"type": "bytes", "sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def redacted_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): redacted_value(item)
            for key, item in value.items()
            if not is_sensitive_key(key)
        }
    if isinstance(value, (list, tuple, set)):
        return [redacted_value(item) for item in value]
    return json_value(value)


def redacted_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): redacted_value(value)
        for key, value in row.items()
        if not is_sensitive_key(key)
    }


def is_sensitive_key(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return bool(SENSITIVE_KEY.fullmatch(normalized))


def value(row: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    normalized = {str(key).lower(): item for key, item in row.items()}
    for name in names:
        if name.lower() in normalized:
            return normalized[name.lower()]
    return default


def integer(raw: Any, default: int | None = None) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def boolean(raw: Any, default: bool = False) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"", "0", "false", "inactive", "disabled", "no"}


def moment(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if raw:
        try:
            parsed = datetime.fromisoformat(str(raw))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def tenant_id_for(row: Mapping[str, Any], by_code: dict[int, int], by_id: dict[int, int]) -> int | None:
    raw = integer(value(row, "TenantId", "tenant_id"))
    if raw is None:
        return None
    # 源 TenantId 是 code，不能回退到另一租户的主键。
    return by_code.get(raw)


def model_provider(raw: Any) -> str:
    text_value = str(raw or "openai_compatible").strip().lower()
    return {"openai": "openai_compatible", "openai_compatible": "openai_compatible", "ollama": "ollama"}.get(text_value, text_value or "custom")


def model_type(raw: Any) -> str:
    text_value = str(raw or "chat").strip().lower()
    return {"1": "chat", "2": "embedding", "3": "rerank"}.get(text_value, text_value or "chat")


async def read_source(source_engine, *, allow_missing: bool) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    async with source_engine.connect() as connection:
        if connection.dialect.name in {"postgresql", "mysql"}:
            await connection.execute(text("SET TRANSACTION READ ONLY"))
        for table in SOURCE_TABLES:
            try:
                async with connection.begin_nested():
                    quoted = connection.dialect.identifier_preparer.quote(table)
                    result = await connection.execute(text("SELECT * FROM " + quoted))
                    tables[table] = [dict(row) for row in result.mappings().all()]
            except SQLAlchemyError as exc:
                errors[table] = type(exc).__name__
                if not allow_missing:
                    raise RuntimeError(f"source table {table} is unavailable") from exc
        await connection.rollback()
    return tables, errors


async def archive_row(
    session: AsyncSession,
    table: str,
    row: Mapping[str, Any],
    tenant_id: int | None,
) -> None:
    payload = redacted_payload(row)
    source_id = source_identity(table, row, payload)
    item = await session.scalar(select(LegacyRecord).where(LegacyRecord.source_table == table, LegacyRecord.source_id == source_id))
    if item is None:
        digest = hashlib.sha256((table + ":" + source_id).encode()).hexdigest()
        archive_id = 1_000_000_000_000_000 + int(digest[:15], 16) % 8_000_000_000_000_000
        while await session.get(LegacyRecord, archive_id) is not None:
            archive_id += 1
        session.add(LegacyRecord(id=archive_id, source_table=table, source_id=source_id, tenant_id=tenant_id, payload=payload))
    else:
        item.tenant_id = tenant_id
        item.payload = payload


async def resource_upsert(
    session: AsyncSession,
    *,
    resource_type: str,
    source_id: int,
    tenant_id: int,
    name: str,
    data: dict[str, Any],
    created_at: Any = None,
    is_deleted: bool = False,
    warnings: list[str] | None = None,
) -> None:
    item = await session.scalar(
        select(Resource).where(
            Resource.resource_type == resource_type,
            Resource.legacy_source_id == source_id,
        )
    )
    if item is not None:
        raise MigrationPreflightError("target_resource_id_collision")
    if item is None:
        item = await session.get(Resource, source_id)
        if item is not None:
            raise MigrationPreflightError("target_resource_id_collision")
    if item is None:
        item = Resource(
            id=source_id,
            tenant_id=tenant_id,
            resource_type=resource_type,
            legacy_source_id=source_id,
            name=name,
            owner_id=None,
            data=data,
            is_deleted=is_deleted,
        )
        session.add(item)
    else:
        item.tenant_id, item.name, item.data, item.is_deleted = tenant_id, name, data, is_deleted
        item.legacy_source_id = source_id
    if item is not None:
        item.is_deleted = is_deleted
    if created_at is not None:
        item.created_at = moment(created_at)


async def import_rows(
    session: AsyncSession,
    tables: dict[str, list[dict[str, Any]]],
    source_errors: dict[str, str],
    *,
    dry_run: bool,
    _preview: bool = False,
) -> dict[str, Any]:
    if dry_run and not _preview:
        raise ValueError("Use run() for a read-only target snapshot preview")
    await preflight_target(session, tables)
    stats: dict[str, dict[str, int]] = {}
    warnings: list[str] = []
    by_code: dict[int, int] = {}
    by_id: dict[int, int] = {}

    def count(table: str, key: str) -> None:
        stats.setdefault(table, {}).setdefault(key, 0)
        stats[table][key] += 1

    async def resolve_tenant_reference(
        model: type[Any],
        raw_id: Any,
        tenant_id: int,
        *,
        table: str,
        source_id: int,
        field: str,
    ) -> int | None:
        candidate = integer(raw_id)
        if candidate is None:
            return None
        item = await session.get(model, candidate)
        if item is None or getattr(item, "tenant_id", None) != tenant_id:
            warnings.append(
                f"{table}/{source_id}: {field}={candidate} is missing or belongs to another tenant; cleared"
            )
            return None
        return candidate

    # Tenants are imported first because all legacy entities store TenantId as
    # the tenant code, while the target uses the tenant primary key.
    for row in tables.get("TTenant", []):
        source_id = integer(value(row, "Id"))
        code = integer(value(row, "Code"))
        if source_id is None or code is None:
            warnings.append("TTenant row skipped: Id or Code is missing")
            count("TTenant", "skipped")
            continue
        item = await session.get(Tenant, source_id)
        if item is None:
            item = Tenant(id=source_id, code=code, name=str(value(row, "Name", default="")), active=boolean(value(row, "Status", default=True)) and not boolean(value(row, "IsDelete")), created_at=moment(value(row, "CreateTime")))
            session.add(item)
        else:
            item.code, item.name, item.active = code, str(value(row, "Name", default=item.name)), boolean(value(row, "Status", default=item.active)) and not boolean(value(row, "IsDelete"))
        by_code[code], by_id[source_id] = source_id, source_id
        count("TTenant", "migrated")
    await session.flush()

    for table, rows in tables.items():
        if table == "TTenant":
            for row in rows:
                await archive_row(session, table, row, by_id.get(integer(value(row, "Id"))))
            continue
        for row in rows:
            await archive_row(session, table, row, tenant_id_for(row, by_code, by_id))

    for row in tables.get("TUser", []):
        source_id = integer(value(row, "Id"))
        tenant_id = tenant_id_for(row, by_code, by_id)
        username = str(value(row, "Name", default="")).strip()
        if source_id is None or tenant_id is None or not username:
            warnings.append("TUser row skipped: Id, tenant, or Name is missing")
            count("TUser", "skipped")
            continue
        password_hash = str(value(row, "PasswordHash", default=""))
        if not password_hash:
            password_hash = hash_password(uuid.uuid4().hex)
            warnings.append(f"TUser/{source_id}: password hash missing; administrator reset required")
        item = await session.get(User, source_id)
        if item is None:
            item = User(id=source_id, tenant_id=tenant_id, username=username, nickname=str(value(row, "NickName", default="")), password_hash=password_hash, is_active=boolean(value(row, "Status", default=True)) and not boolean(value(row, "IsDelete")), is_super_admin=boolean(value(row, "IsSuperAdmin")), created_at=moment(value(row, "CreateTime")))
            session.add(item)
        else:
            item.tenant_id, item.username, item.nickname, item.password_hash = tenant_id, username, str(value(row, "NickName", default=item.nickname)), password_hash
            item.is_active, item.is_super_admin = boolean(value(row, "Status", default=item.is_active)) and not boolean(value(row, "IsDelete")), boolean(value(row, "IsSuperAdmin", default=item.is_super_admin))
        count("TUser", "migrated")
    await session.flush()

    for row in tables.get("TRole", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TRole", "skipped")
            continue
        item = await session.get(Role, source_id)
        if item is None:
            session.add(Role(id=source_id, tenant_id=tenant_id, name=str(value(row, "Name", default="")), description=str(value(row, "Remarks", "Description", default=""))))
        else:
            item.tenant_id, item.name, item.description = tenant_id, str(value(row, "Name", default=item.name)), str(value(row, "Remarks", "Description", default=item.description))
        count("TRole", "migrated")
    await session.flush()

    for row in tables.get("TPermission", []):
        permission_id = str(value(row, "Id", default="")).strip()
        if not permission_id:
            count("TPermission", "skipped")
            continue
        item = await session.get(Permission, permission_id)
        name = str(value(row, "FullName", "ActionName", default=permission_id))
        if item is None:
            session.add(Permission(id=permission_id, code=permission_id, name=name, category=str(value(row, "ModuleName", "AreaName", default="api"))))
        else:
            item.name, item.category = name, str(value(row, "ModuleName", "AreaName", default=item.category))
        count("TPermission", "migrated")
    await session.flush()

    for row in tables.get("TUserBindRole", []):
        user_id, role_id = integer(value(row, "UserId")), integer(value(row, "RoleId"))
        user = await session.get(User, user_id) if user_id is not None else None
        role = await session.get(Role, role_id) if role_id is not None else None
        if user is None or role is None or user.tenant_id != role.tenant_id:
            count("TUserBindRole", "skipped")
            continue
        if await session.scalar(select(UserRole).where(UserRole.user_id == user_id, UserRole.role_id == role_id)) is None:
            session.add(UserRole(user_id=user_id, role_id=role_id))
        count("TUserBindRole", "migrated")

    for row in tables.get("TRolePermission", []):
        role_id, permission_id = integer(value(row, "RoleId")), str(value(row, "PermissionId", default=""))
        if role_id is None or not permission_id or await session.get(Role, role_id) is None or await session.get(Permission, permission_id) is None:
            count("TRolePermission", "skipped")
            continue
        if await session.scalar(select(RolePermission).where(RolePermission.role_id == role_id, RolePermission.permission_id == permission_id)) is None:
            session.add(RolePermission(role_id=role_id, permission_id=permission_id))
        count("TRolePermission", "migrated")

    for row in tables.get("TAIModels", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TAIModels", "skipped")
            continue
        item = await session.get(AIModel, source_id)
        fields = {
            "tenant_id": tenant_id, "provider": model_provider(value(row, "AIType")), "model_type": model_type(value(row, "AIModelType")),
            "endpoint": str(value(row, "EndPoint", default="")), "model_name": str(value(row, "ModelName", default="")),
            "description": str(value(row, "ModelDescription", default="")), "embedding_dimensions": integer(value(row, "EmbeddingValueSize")),
            "max_input_tokens": integer(value(row, "MaxAskPromptSize"), 131072) or 131072, "max_output_tokens": integer(value(row, "AnswerTokens"), 8192) or 8192,
            "secret_encrypted": None, "secret_hint": None, "enabled": not boolean(value(row, "IsDelete")),
        }
        if item is None:
            session.add(AIModel(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
        count("TAIModels", "migrated")

    await session.flush()

    for row in tables.get("TAIPrompts", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TAIPrompts", "skipped")
            continue
        content = str(value(row, "Prompt", "Content", default=""))
        item = await session.get(AIPrompt, source_id)
        if item is None:
            session.add(AIPrompt(id=source_id, tenant_id=tenant_id, name=str(value(row, "Name", default="")), content=content, enabled=not boolean(value(row, "IsDelete"))))
        else:
            item.tenant_id, item.name, item.content = tenant_id, str(value(row, "Name", default=item.name)), content
        await resource_upsert(session, resource_type="ai_prompts", source_id=source_id, tenant_id=tenant_id, name=str(value(row, "Name", default="")), data={"content": content, "description": value(row, "Description", default="")}, created_at=value(row, "CreateTime"), is_deleted=boolean(value(row, "IsDelete")), warnings=warnings)
        count("TAIPrompts", "migrated")

    for row in tables.get("TAIApps", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TAIApps", "skipped")
            continue
        app_data = {
            "describe": value(row, "Describe", "Description", default=""), "icon": value(row, "Icon", default="windows"), "type": value(row, "Type", default="chat"),
            "chatModelId": integer(value(row, "ChatModelID", "ChatModelId")), "rerankModelId": integer(value(row, "RerankModelID", "RerankModelId")),
            "knowledgeBaseId": integer(value(row, "KmsId")), "promptId": integer(value(row, "AIPromptID", "AIPromptId")), "temperature": value(row, "Temperature", default=50),
            "maxMatchesCount": value(row, "MaxMatchesCount", default=3), "rerankCount": value(row, "RerankCount", default=20), "maxRetries": value(row, "MaxRetries", default=2),
            "networkTimeout": value(row, "NetworkTimeout", default=30), "authorizedDomains": value(row, "AuthorizedDomains", default="*"), "chatMessageLimit": value(row, "ChatMessageLimit", default=100),
            "isAITools": boolean(value(row, "IsAITools", default=True)), "isSkill": boolean(value(row, "IsSkill", default=True)), "isMcp": boolean(value(row, "IsMcp", default=True)), "isMemory": boolean(value(row, "IsMemory")),
            "secretConfigured": False,
        }
        app_secret = value(row, "SecretKey")
        if app_secret not in (None, ""):
            app_data["secret_encrypted"] = encrypt_secret(str(app_secret))
            app_data["secretConfigured"] = True
        app_data["chatModelId"] = await resolve_tenant_reference(
            AIModel,
            app_data["chatModelId"],
            tenant_id,
            table="TAIApps",
            source_id=source_id,
            field="ChatModelID",
        )
        app_data["rerankModelId"] = await resolve_tenant_reference(
            AIModel,
            app_data["rerankModelId"],
            tenant_id,
            table="TAIApps",
            source_id=source_id,
            field="RerankModelID",
        )
        app_data["promptId"] = await resolve_tenant_reference(
            AIPrompt,
            app_data["promptId"],
            tenant_id,
            table="TAIApps",
            source_id=source_id,
            field="AIPromptID",
        )
        item = await session.get(AIApp, source_id)
        dedicated = {"tenant_id": tenant_id, "name": str(value(row, "Name", default="")), "description": str(app_data["describe"]), "chat_model_id": app_data["chatModelId"], "prompt_id": app_data["promptId"], "knowledge_base_id": app_data["knowledgeBaseId"], "config": app_data, "enabled": not boolean(value(row, "IsDelete"))}
        if item is None:
            session.add(AIApp(id=source_id, **dedicated))
        else:
            for key, field_value in dedicated.items():
                setattr(item, key, field_value)
        await resource_upsert(session, resource_type="ai_apps", source_id=source_id, tenant_id=tenant_id, name=str(value(row, "Name", default="")), data=app_data, created_at=value(row, "CreateTime"), is_deleted=boolean(value(row, "IsDelete")), warnings=warnings)
        count("TAIApps", "migrated")

    for row in tables.get("TAIKmss", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TAIKmss", "skipped")
            continue
        config = {"maxTokensPerParagraph": value(row, "MaxTokensPerParagraph", default=299), "maxTokensPerLine": value(row, "MaxTokensPerLine", default=99), "overlappingTokens": value(row, "OverlappingTokens", default=49)}
        item = await session.get(KnowledgeBase, source_id)
        fields = {"tenant_id": tenant_id, "name": str(value(row, "Name", default="")), "embedding_model_id": await resolve_tenant_reference(AIModel, value(row, "aIModelsId", "AIModelsId"), tenant_id, table="TAIKmss", source_id=source_id, field="AIModelsId"), "rerank_model_id": await resolve_tenant_reference(AIModel, value(row, "aIRerankModelsId", "AIRerankModelsId"), tenant_id, table="TAIKmss", source_id=source_id, field="AIRerankModelsId"), "chunk_size": 1200, "chunk_overlap": 180, "status": "ready"}
        if item is None:
            session.add(KnowledgeBase(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
        await resource_upsert(session, resource_type="knowledge_bases", source_id=source_id, tenant_id=tenant_id, name=fields["name"], data={**config, "embeddingModelId": fields["embedding_model_id"], "rerankModelId": fields["rerank_model_id"], "fileIds": []}, created_at=value(row, "CreateTime"), is_deleted=boolean(value(row, "IsDelete")), warnings=warnings)
        count("TAIKmss", "migrated")

    # Knowledge bases are imported after applications because the legacy app
    # row may reference a KB that was listed later in the source dump. Resolve
    # those optional references only after all KB rows are flushed.
    await session.flush()
    for row in tables.get("TAIApps", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            continue
        item = await session.get(AIApp, source_id)
        if item is None:
            continue
        knowledge_base_id = await resolve_tenant_reference(
            KnowledgeBase,
            value(row, "KmsId"),
            tenant_id,
            table="TAIApps",
            source_id=source_id,
            field="KmsId",
        )
        item.knowledge_base_id = knowledge_base_id
        item.config = {**(item.config or {}), "knowledgeBaseId": knowledge_base_id}
        resource = await session.scalar(
            select(Resource).where(
                Resource.resource_type == "ai_apps",
                Resource.legacy_source_id == source_id,
            )
        )
        if resource is not None:
            resource.data = {**(resource.data or {}), "knowledgeBaseId": knowledge_base_id}

    for row in tables.get("TFile", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        if source_id is None or tenant_id is None:
            count("TFile", "skipped")
            continue
        file_data = {"path": value(row, "Path", default=""), "sourceUrl": value(row, "Url", default=""), "table": value(row, "Table", default=""), "tableId": value(row, "TableId", default=""), "sign": value(row, "Sign", default=""), "sort": value(row, "Sort", default=0), "migrationStatus": "pending_file_copy"}
        await resource_upsert(session, resource_type="files", source_id=source_id, tenant_id=tenant_id, name=str(value(row, "Name", default=f"{source_id}.bin")), data=file_data, created_at=value(row, "CreateTime"), is_deleted=boolean(value(row, "IsDelete")), warnings=warnings)
        count("TFile", "migrated")

    for row in tables.get("TAIKmsDetails", []):
        source_id, tenant_id, kb_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id), integer(value(row, "KmsId"))
        knowledge_base = await session.get(KnowledgeBase, kb_id) if kb_id is not None else None
        if source_id is None or tenant_id is None or knowledge_base is None or knowledge_base.tenant_id != tenant_id:
            if source_id is not None and kb_id is not None:
                warnings.append(f"TAIKmsDetails/{source_id}: KmsId={kb_id} is missing or belongs to another tenant; skipped")
            count("TAIKmsDetails", "skipped")
            continue
        item = await session.get(KnowledgeDocument, source_id)
        raw_file_id = integer(value(row, "FileId"))
        file = await session.scalar(select(Resource).where(
            Resource.resource_type == "files", Resource.legacy_source_id == raw_file_id,
            Resource.tenant_id == tenant_id,
        )) if raw_file_id is not None else None
        if raw_file_id is not None and file is None:
            warnings.append(f"TAIKmsDetails/{source_id}: FileId={raw_file_id} unavailable; cleared")
        fields = {"tenant_id": tenant_id, "knowledge_base_id": kb_id, "file_id": file.id if file is not None else None, "source_name": str(value(row, "ContentName", "FileType", default=f"document-{source_id}")), "content": str(value(row, "Content", default="")), "status": "imported", "error_message": str(value(row, "ErrorMessage", default=""))}
        if item is None:
            session.add(KnowledgeDocument(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
        count("TAIKmsDetails", "migrated")

    for row in tables.get("TAIChats", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        owner_id = await resolve_tenant_reference(User, value(row, "UserId"), tenant_id, table="TAIChats", source_id=source_id or 0, field="UserId") if tenant_id is not None and source_id is not None else None
        app_id = await resolve_tenant_reference(AIApp, value(row, "AppId"), tenant_id, table="TAIChats", source_id=source_id or 0, field="AppId") if tenant_id is not None and source_id is not None else None
        if source_id is None or tenant_id is None or owner_id is None:
            count("TAIChats", "skipped")
            continue
        item = await session.get(ChatSession, source_id)
        fields = {"tenant_id": tenant_id, "owner_id": owner_id, "app_id": app_id, "title": str(value(row, "Name", default=""))}
        if item is None:
            session.add(ChatSession(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
        await resource_upsert(session, resource_type="chats", source_id=source_id, tenant_id=tenant_id, name=fields["title"], data={"userId": owner_id, "appId": app_id, "lastMessage": value(row, "LastMessage", default=""), "hidden": boolean(value(row, "IsHidden"))}, created_at=value(row, "CreateTime"), is_deleted=boolean(value(row, "IsDelete")), warnings=warnings)
        count("TAIChats", "migrated")

    await session.flush()
    for row in tables.get("TAIChatHistorys", []):
        source_id, tenant_id, session_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id), integer(value(row, "AIChatsId"))
        chat_session = await session.get(ChatSession, session_id) if session_id is not None else None
        if source_id is None or tenant_id is None or chat_session is None or chat_session.tenant_id != tenant_id:
            count("TAIChatHistorys", "skipped")
            continue
        if await session.get(ChatMessage, source_id) is None:
            input_count, output_count = integer(value(row, "InputTokenCount"), 0) or 0, integer(value(row, "OutputTokenCount"), 0) or 0
            session.add(ChatMessage(id=source_id, tenant_id=tenant_id, session_id=session_id, role="user" if boolean(value(row, "IsSend")) else "assistant", content=str(value(row, "Content", default="")), request_id=None, token_usage={"input": input_count, "output": output_count, "total": input_count + output_count}, status="completed", created_at=moment(value(row, "CreateTime"))))
        count("TAIChatHistorys", "migrated")

    for row in tables.get("TAIAgentMemory", []):
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        owner_id = await resolve_tenant_reference(User, value(row, "UserId"), tenant_id, table="TAIAgentMemory", source_id=source_id or 0, field="UserId") if tenant_id is not None and source_id is not None else None
        if source_id is None or tenant_id is None or owner_id is None:
            count("TAIAgentMemory", "skipped")
            continue
        item = await session.get(AgentMemory, source_id)
        agent_id = await resolve_tenant_reference(AIApp, value(row, "AIAppsId"), tenant_id, table="TAIAgentMemory", source_id=source_id, field="AIAppsId")
        fields = {"tenant_id": tenant_id, "owner_id": owner_id, "agent_id": agent_id, "content": str(value(row, "Content", default="")), "attributes": {"memoryType": value(row, "MemoryType", default="other"), "keywords": value(row, "Keywords", default=""), "importance": value(row, "Importance", default=5), "expireTime": json_value(value(row, "ExpireTime"))}}
        if item is None:
            session.add(AgentMemory(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
        count("TAIAgentMemory", "migrated")

    department_rows = tables.get("TDepartment", [])
    for row in department_rows:
        await _import_organization_row(session, "TDepartment", row, by_code, by_id, count, warnings=warnings, defer_parent=True)
    await session.flush()
    for row in department_rows:
        source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
        parent_id = integer(value(row, "ParentId"))
        item = await session.get(Department, source_id) if source_id is not None else None
        parent = await session.get(Department, parent_id) if parent_id is not None else None
        if item is None or tenant_id is None:
            continue
        if parent_id is None or parent_id == source_id:
            item.parent_id = None
        elif parent is None or parent.tenant_id != tenant_id:
            item.parent_id = None
            warnings.append(f"TDepartment/{source_id}: ParentId={parent_id} is missing or belongs to another tenant; cleared")
        else:
            item.parent_id = parent_id

    for table, rows in (("TPosition", tables.get("TPosition", [])),):
        for row in rows:
            await _import_organization_row(session, table, row, by_code, by_id, count, warnings=warnings)

    await session.flush()
    integrity = await verify_integrity(session, tables, source_errors, redacted_payload)
    for table, checks in integrity["tables"].items():
        stats.setdefault(table, {"migrated": 0, "skipped": 0}).update(checks)
    if integrity["fatal_errors"]:
        await session.rollback()
        raise MigrationIntegrityError(integrity)
    if dry_run:
        await session.rollback()
    else:
        await session.commit()
    complete = integrity["complete"] and not warnings and not any(row.get("skipped", 0) for row in stats.values())
    return {"tables": stats, "warnings": warnings, "source_errors": source_errors,
            "integrity": integrity, "status": "success" if complete else "partial",
            "transaction": "preview_memory_rolled_back" if dry_run else "committed"}


async def _import_organization_row(
    session: AsyncSession,
    table: str,
    row: Mapping[str, Any],
    by_code: dict[int, int],
    by_id: dict[int, int],
    count: Any,
    *,
    warnings: list[str],
    defer_parent: bool = False,
) -> None:
    source_id, tenant_id = integer(value(row, "Id")), tenant_id_for(row, by_code, by_id)
    if source_id is None or tenant_id is None:
        count(table, "skipped")
        return
    if table == "TDepartment":
        item = await session.get(Department, source_id)
        fields = {"tenant_id": tenant_id, "parent_id": None if defer_parent else integer(value(row, "ParentId")), "name": str(value(row, "Name", default="")), "active": not boolean(value(row, "IsDelete")), "created_at": moment(value(row, "CreateTime"))}
        if item is None:
            session.add(Department(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
    else:
        item = await session.get(Position, source_id)
        fields = {"tenant_id": tenant_id, "name": str(value(row, "Name", default="")), "description": str(value(row, "Description", default="")), "active": not boolean(value(row, "IsDelete")), "created_at": moment(value(row, "CreateTime"))}
        if item is None:
            session.add(Position(id=source_id, **fields))
        else:
            for key, field_value in fields.items():
                setattr(item, key, field_value)
    count(table, "migrated")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "source": safe_url(args.source_url), "target": safe_url(args.target_url), "dry_run": args.dry_run, "status": "failed"}
    source_engine = target_engine = None
    try:
        if args.dry_run and args.prepare_schema:
            raise ValueError("dry-run and prepare-schema are mutually exclusive")
        if database_identity(args.source_url) == database_identity(args.target_url):
            raise ValueError("Source and target must be separate databases")
        source_engine = await read_only_engine(args.source_url)
        tables, source_errors = await read_source(source_engine, allow_missing=args.allow_missing_tables)
        # 源冲突须在 prepare-schema 的 DDL 之前拒绝。
        source_resource_scopes(tables)
        target_engine = await preview_engine(args.target_url) if args.dry_run else create_async_engine(args.target_url, pool_pre_ping=True)
        if args.prepare_schema:
            async with target_engine.begin() as connection:
                if await connection.run_sync(lambda c: inspect(c).get_table_names()):
                    raise MigrationPreflightError("prepare_schema_requires_empty_target")
                await connection.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(target_engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            result = await import_rows(session, tables, source_errors, dry_run=args.dry_run, _preview=args.dry_run)
        report.update(result)
        report["model_keys_discarded"] = len(tables.get("TAIModels", []))
        report["application_secrets_encrypted"] = sum(
            1 for row in tables.get("TAIApps", []) if value(row, "SecretKey") not in (None, "")
        )
        report["validation_scope"] = "documented entity/relation mapping and redacted archives; not application readiness"
        return report
    except MigrationPreflightError as exc:
        report.update(error="MigrationPreflightError", error_code=exc.code,
                      transaction="not_committed", migration_blocked=True)
        return report
    except MigrationIntegrityError as exc:
        report.update(error="MigrationIntegrityError", integrity=exc.integrity, transaction="rolled_back")
        return report
    except Exception as exc:
        # 驱动异常可能包含连接口令、绑定参数或原始密钥。
        report.update(error=type(exc).__name__, transaction="not_committed")
        return report
    finally:
        if source_engine is not None:
            await source_engine.dispose()
        if target_engine is not None:
            await target_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--report", type=Path, default=Path(__file__).parents[1] / "reports" / "mysql_migration_report.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prepare-schema", action="store_true")
    parser.add_argument("--allow-missing-tables", action="store_true")
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        report = {"generated_at": datetime.now(UTC).isoformat(), "source": safe_url(args.source_url), "target": safe_url(args.target_url), "status": "failed", "error": type(exc).__name__}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(2) from exc
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "failed":
        raise SystemExit(2)
    if report["status"] == "partial":
        raise SystemExit(3)


if __name__ == "__main__":
    main()

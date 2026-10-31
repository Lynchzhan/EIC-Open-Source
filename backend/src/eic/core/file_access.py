"""文件对象授权；缺少 accessMode 的历史记录保留租户读取兼容，不认领 owner。"""

from pathlib import Path
import re
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, false, func, or_, text

from eic.core.config import get_settings
from eic.models import Resource


def file_access_mode(item: Resource) -> str:
    data = item.data if item.data is not None else {}
    if not isinstance(data, dict):
        return "invalid"
    if "accessMode" not in data:
        return "legacy"
    mode = data["accessMode"]
    return mode if isinstance(mode, str) and mode in {"private", "tenant"} else "invalid"


def can_access_file(item: Resource | None, tenant_id: int, user_id: int | None, *, write: bool = False) -> bool:
    if item is None or item.tenant_id != tenant_id or item.resource_type != "files" or item.is_deleted:
        return False
    mode = file_access_mode(item)
    if mode == "invalid":
        return False
    if write:
        # 历史数据不被静默私有化；旧租户写语义留给独立迁移决策。
        return mode == "legacy" or (user_id is not None and item.owner_id == user_id)
    return mode in {"tenant", "legacy"} or (user_id is not None and item.owner_id == user_id)


def can_bind_shared_file(item: Resource | None, tenant_id: int) -> bool:
    return can_access_file(item, tenant_id, None) and file_access_mode(item) in {"tenant", "legacy"}


def readable_file_clause(session, user_id):
    """与 can_access_file 的 legacy/null/invalid 语义一致，再由 SQL 分页。"""
    dialect = session.get_bind().dialect.name
    field = Resource.data['accessMode']
    if dialect == 'sqlite':
        root_type = func.json_type(Resource.data)
        mode_type = func.json_type(Resource.data, '$.accessMode')
        object_type, string_type, null_type = 'object', 'text', 'null'
    elif dialect in {'mysql', 'mariadb'}:
        root_type = func.json_type(Resource.data)
        mode_type = func.json_type(func.json_extract(Resource.data, '$.accessMode'))
        object_type, string_type, null_type = 'OBJECT', 'STRING', 'NULL'
    elif dialect == 'postgresql':
        root_type = func.json_typeof(Resource.data)
        mode_type = func.json_typeof(field)
        object_type, string_type, null_type = 'object', 'string', 'null'
    else:
        raise ValueError('unsupported_file_authorization_dialect')
    mode = field.as_string()
    legacy = or_(root_type.is_(None), root_type == null_type,
                 and_(root_type == object_type, mode_type.is_(None)))
    valid = and_(root_type == object_type, mode_type == string_type,
                 or_(mode == 'tenant', and_(mode == 'private',
                    Resource.owner_id == user_id if user_id is not None else false())))
    return or_(legacy, valid)


def valid_file_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件 ID 无效"})
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,19}", text) or not 0 < int(text) < 2**63:
        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "文件 ID 无效"})
    return int(text)


async def require_file_access(session, principal, value: Any, *, write: bool = False, shared: bool = False) -> Resource:
    file_id = valid_file_id(value)
    if (write or shared) and session.get_bind().dialect.name == "sqlite":
        # SQLite 忽略 FOR UPDATE；在授权读取前取得事务写锁，直至引用或共享更新提交。
        # 使用原始无值变更 UPDATE，避免触发 SQLAlchemy 的 updated_at/onupdate。
        await session.execute(
            text("UPDATE resources SET id = id WHERE id = :file_id AND tenant_id = :tenant_id AND resource_type = 'files'"),
            {"file_id": file_id, "tenant_id": principal.tenant_id},
        )
    # 支持行锁的数据库仍用 FOR UPDATE，并刷新 identity map 中的旧授权状态。
    options = {"with_for_update": True, "populate_existing": True} if write or shared else {}
    item = await session.get(Resource, file_id, **options)
    if not can_access_file(item, principal.tenant_id, principal.user_id, write=write):
        raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "文件不存在"})
    if shared and not can_bind_shared_file(item, principal.tenant_id):
        raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "请先显式共享文件，再绑定共享知识库或技能"})
    return item


def file_source_path(item: Resource) -> Path:
    source = Path(str((item.data or {}).get("path", ""))).resolve()
    settings = get_settings()
    root = settings.runtime_path(settings.upload_root).resolve()
    if root not in source.parents or not source.is_file():
        raise ValueError("file source is unavailable or outside the configured upload root")
    return source

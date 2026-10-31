"""头像使用统一上传预占、原子发布与失败补偿。"""
import base64
from io import BytesIO
import logging
from pathlib import Path
import re

import anyio
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from sqlalchemy import String, cast, select
from eic.core.uploads import compensate, save_uploads
from eic.models import ChatMessage, KnowledgeDocument, Resource, User

MAX_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 4_000_000


def normalize_avatar(value):
    header, separator, encoded = value.partition(",")
    if (not separator or header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}
            or len(encoded) > ((MAX_BYTES + 2) // 3) * 4):
        raise ValueError("invalid_avatar")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > MAX_BYTES:
        raise ValueError("avatar_too_large")
    from PIL import Image
    with Image.open(BytesIO(data)) as image:
        if image.width * image.height > MAX_PIXELS or image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("invalid_avatar")
        output = BytesIO()
        image.convert("RGB").save(output, format="JPEG", quality=90)
    result = output.getvalue()
    if len(result) > MAX_BYTES:
        raise ValueError("avatar_too_large")
    return result


async def save_avatar(value, principal, session, root, settings, sessions, lock_tenant):
    try:
        data = await anyio.to_thread.run_sync(normalize_avatar, value)
    except Exception:
        raise HTTPException(400, detail={"code": 400, "errMsg": "头像需为不超过 2 MiB、400 万像素的有效 PNG/JPEG/WebP 图片"}) from None
    file = UploadFile(BytesIO(data), filename="avatar.jpg", size=len(data),
                      headers=Headers({"content-type": "image/jpeg"}))
    try:
        rows = await save_uploads([file], principal, session, root, settings, sessions, lock_tenant, commit=False)
        resource = await session.get(Resource, rows[0]["id"])
        resource.data = {**resource.data, "generatedAvatar": True}
        return rows[0]["url"]
    finally:
        await file.close()


async def compensate_profile_uploads(session, sessions, lock_tenant):
    staged = session.info.pop("stagedUploads", [])
    session.info.pop("profileUploadTransaction", None)
    await session.rollback()
    for token, owned, root, tenant_id in staged:
        await compensate(token, owned, root, tenant_id, sessions, lock_tenant)


async def retire_previous_avatar(url, owner_id, tenant_id, root, sessions, lock_tenant):
    match = re.fullmatch(r"/api/File/GetFile\?fileid=([0-9]+)", url or "")
    if match is None:
        return
    try:
        identity = int(match[1])
        async with sessions() as session:
            await lock_tenant(session, tenant_id)
            row = await session.get(Resource, identity, populate_existing=True, with_for_update=True)
            if (row is None or row.tenant_id != tenant_id or row.owner_id != owner_id
                    or row.resource_type != "files" or (row.data or {}).get("generatedAvatar") is not True):
                return
            if await session.scalar(select(User.id).where(User.avatar_url == url).limit(1)):
                return
            if await session.scalar(select(User.id).where(cast(User.profile, String).contains(str(identity))).limit(1)):
                return
            if await session.scalar(select(KnowledgeDocument.id).where(KnowledgeDocument.file_id == identity).limit(1)):
                return
            if await session.scalar(select(Resource.id).where(Resource.id != identity,
                    cast(Resource.data, String).contains(str(identity))).limit(1)):
                return
            if await session.scalar(select(ChatMessage.id).where(ChatMessage.content.contains(url)).limit(1)):
                return
            path = Path(row.data["path"])
            resolved_root = Path(root).resolve()
            if path.is_symlink() or path.resolve().parent != resolved_root:
                return
            await session.delete(row)
            await session.commit()
        await anyio.to_thread.run_sync(lambda: path.unlink(missing_ok=True))
    except Exception:
        logging.getLogger(__name__).warning("旧头像回收未完成，保留待核对；不影响资料保存")

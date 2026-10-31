"""分块上传、持久预占与保守补偿；不按时间自动释放未知状态的预占。"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import uuid

import anyio
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import delete, select
from starlette.formparsers import MultiPartException

from eic.core.ids import new_id
from eic.models import Resource, UploadReservation

logger = logging.getLogger(__name__)
LIMIT_FIELDS = (
    "max_file_bytes", "max_request_bytes", "max_body_bytes", "max_files",
    "tenant_max_bytes", "tenant_max_files",
)
UPLOAD_ROUTES = {"/api/File/UploadFile", "/api/File/BatchUploadFile"}
PROFILE_ROUTES = {"/api/User/AddEdit", "/api/User/EditUser"}
PROFILE_BODY_BYTES = 3 * 1024 * 1024


class UploadBodyTooLarge(HTTPException, MultiPartException):
    def __init__(self):
        HTTPException.__init__(self, status_code=413, detail="上传请求体超过配置限额")
        self.message = "上传请求体超过配置限额"


def reject(status: int, code: str, message: str):
    raise HTTPException(status_code=status, detail={"code": status, "errMsg": message, "reason": code})


def limits(settings):
    return {name: getattr(settings, "upload_" + name) for name in LIMIT_FIELDS}


def bound(value, maximum, code):
    if maximum is not None and value > maximum:
        reject(413, code, "上传超过配置限额")


class UploadBodyLimit:
    """在 multipart 解析前检查整个 HTTP body；Content-Length 不是唯一证据。"""
    def __init__(self, app, settings):
        self.app, self.settings = app, settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path")
        maximum = PROFILE_BODY_BYTES if path in PROFILE_ROUTES else self.settings.upload_max_body_bytes
        if path not in UPLOAD_ROUTES | PROFILE_ROUTES or maximum is None:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > maximum:
            return await JSONResponse(status_code=413, content={
                "code": 413, "errMsg": "上传请求体超过配置限额", "reason": "body_bytes",
            })(scope, receive, send)
        received = 0
        exceeded = False

        async def bounded_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > maximum:
                    exceeded = True
                    # multipart parser 会关闭已创建的临时文件；裸 body 也保留 HTTP 异常语义。
                    raise UploadBodyTooLarge()
            return message

        async def bounded_send(message):
            if exceeded and message["type"] == "http.response.start":
                # Request.form 将 MultiPartException 转为 400；此处恢复准确的 413 状态。
                message = {**message, "status": 413}
            await send(message)

        await self.app(scope, bounded_receive, bounded_send)


def reservation_size(files, settings):
    if not files:
        reject(422, "empty_batch", "请选择上传文件")
    bound(len(files), settings.upload_max_files, "file_count")
    sizes = []
    for file in files:
        size = file.size
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            # HTTP multipart 的 size 来自解析器；直接调用时未知大小必须保守预占。
            size = settings.upload_max_file_bytes
            if size is None:
                if any(getattr(settings, "upload_" + name) is not None for name in
                       ("max_request_bytes", "tenant_max_bytes")):
                    reject(411, "unknown_size", "配额上传需要已知大小或单文件上限")
                sizes.append(None)
                continue
        bound(size, settings.upload_max_file_bytes, "file_bytes")
        sizes.append(size)
    total = sum(size or 0 for size in sizes)
    bound(total, settings.upload_max_request_bytes, "request_bytes")
    return total, sizes


async def reserve(session, principal, files, settings, token, lock_tenant, *, commit=True):
    total, sizes = reservation_size(files, settings)
    if commit:
        await lock_tenant(session, principal.tenant_id)
    elif session.info.get("profileUploadTransaction") != (principal.tenant_id, session.sync_session.get_transaction()):
        raise RuntimeError("外部事务上传必须先在同一事务持有租户写锁")
    rows = list(await session.scalars(select(Resource).where(
        Resource.tenant_id == principal.tenant_id, Resource.resource_type == "files",
    )))
    # 软删除保留物理文件，所以仍计入配额；不偷偷清理以释放容量。
    used_bytes = 0
    for row in rows:
        size = (row.data or {}).get("size") if isinstance(row.data, dict) else None
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            if settings.upload_tenant_max_bytes is not None:
                reject(503, "historical_size_unknown", "历史文件大小未核验，租户配额暂停分配")
        else:
            used_bytes += size
    pending = list(await session.scalars(select(UploadReservation).where(
        UploadReservation.tenant_id == principal.tenant_id,
    )))
    if any(row.reserved_bytes < 0 or row.file_count < 1 for row in pending):
        reject(503, "reservation_invalid", "上传预占数据异常，请先核验")
    bound(used_bytes + sum(row.reserved_bytes for row in pending) + total,
          settings.upload_tenant_max_bytes, "tenant_bytes")
    bound(len(rows) + sum(row.file_count for row in pending) + len(files),
          settings.upload_tenant_max_files, "tenant_files")
    session.add(UploadReservation(token=token, tenant_id=principal.tenant_id,
                                  reserved_bytes=total, file_count=len(files)))
    if commit:
        await session.commit()
    else:
        await session.flush()
    return sizes


def create_pending(root, token, suffix):
    path = root / (".pending-" + token + "-" + uuid.uuid4().hex + suffix)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return path, fd


def publish(path, destination, owned):
    # 同文件系统硬链接提供原子且禁止覆盖的发布；不使用 replace 覆盖既有文件。
    os.link(path, destination)
    owned.append(destination)
    path.unlink()


async def compensate(token, owned, root, tenant_id, session_factory, lock_tenant):
    try:
        async with session_factory() as check:
            await lock_tenant(check, tenant_id)
            rows = list(await check.scalars(select(Resource).where(Resource.resource_type == "files")))
            referenced = {
                Path(row.data["path"]).resolve() for row in rows
                if isinstance(row.data, dict) and isinstance(row.data.get("path"), str)
            }
            for path in owned:
                if path.parent != root or path.resolve() in referenced:
                    continue
                await anyio.to_thread.run_sync(lambda p=path: p.unlink(missing_ok=True))
            await check.execute(delete(UploadReservation).where(
                UploadReservation.token == token, UploadReservation.tenant_id == tenant_id,
            ))
            await check.commit()
    except Exception:
        # 不确定 commit 是否成功或磁盘清理失败时保留文件/预占，等待只读核验。
        logger.exception("upload compensation incomplete token=%s", token)


async def save_uploads(files, principal, session, root, settings, session_factory, lock_tenant, *, commit=True):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    owned = []
    staged = []
    total = 0
    try:
        sizes = await reserve(session, principal, files, settings, token, lock_tenant, commit=commit)
        for file, reserved in zip(files, sizes):
            suffix = Path(file.filename or "upload").suffix
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
                suffix = ".bin"
            pending, fd = await anyio.to_thread.run_sync(create_pending, root, token, suffix)
            owned.append(pending)
            written = 0
            # fdopen 不重新打开路径；写、flush、fsync 不阻塞事件循环。
            handle = os.fdopen(fd, "wb")
            try:
                while chunk := await file.read(settings.upload_chunk_bytes):
                    written += len(chunk)
                    total += len(chunk)
                    bound(written, settings.upload_max_file_bytes, "file_bytes")
                    bound(total, settings.upload_max_request_bytes, "request_bytes")
                    if reserved is not None and written > reserved:
                        reject(413, "size_changed", "实际文件大小超过预占")
                    await anyio.to_thread.run_sync(handle.write, chunk)
                await anyio.to_thread.run_sync(handle.flush)
                await anyio.to_thread.run_sync(os.fsync, handle.fileno())
            finally:
                await anyio.to_thread.run_sync(handle.close)
            final = root / (uuid.uuid4().hex + suffix)
            staged.append((file, pending, final, written))
        if commit:
            await lock_tenant(session, principal.tenant_id)
        reservation = await session.get(UploadReservation, token)
        if reservation is None or reservation.tenant_id != principal.tenant_id:
            reject(409, "reservation_missing", "上传预占不存在，请核验上传状态")
        results = []
        for file, pending, final, written in staged:
            await anyio.to_thread.run_sync(publish, pending, final, owned)
            file_id = await new_id(session)
            session.add(Resource(
                id=file_id, tenant_id=principal.tenant_id, resource_type="files",
                name=file.filename or final.name, owner_id=principal.user_id,
                data={"path": str(final), "size": written,
                      "contentType": file.content_type or "application/octet-stream",
                      "accessMode": "private", "uploadToken": token},
            ))
            results.append({"id": file_id, "name": file.filename, "size": written,
                            "accessMode": "private", "url": f"/api/File/GetFile?fileid={file_id}"})
        await session.delete(reservation)
        if commit:
            await session.commit()
        else:
            await session.flush()
            session.info.setdefault("stagedUploads", []).append((token, list(owned), root, principal.tenant_id))
        return results
    except BaseException:
        # 先结束失败事务，再独立核验持久引用。取消也进入补偿，绝不盲删已提交文件。
        with anyio.CancelScope(shield=True):
            await session.rollback()
            task = asyncio.create_task(compensate(
                token, owned, root, principal.tenant_id, session_factory, lock_tenant,
            ))
            await asyncio.shield(task)
        raise

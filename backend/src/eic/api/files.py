"""files 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends
from fastapi import File
from fastapi import HTTPException
from eic.api.schemas import PageInput as PageInput
from eic.core.authentication import Principal
from fastapi import Request
from eic.models import Resource
from fastapi import UploadFile
from eic.core.file_access import readable_file_clause
from eic.core.file_access import file_access_mode
from eic.core.database import get_session
from eic.core.file_access import require_file_access
from sqlalchemy import func, select
from eic.core.uploads import limits as upload_limits
from fastapi.responses import FileResponse


@dataclass(frozen=True)
class Dependencies:
    FileResponse: Callable[..., Any]
    attach_operation_logs: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    file_source_path: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    page_result: Callable[..., Any]
    require_no_shared_file_references: Callable[..., Any]
    save_uploads: Callable[..., Any]
    serialize_file: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    @app.post("/api/File/UploadFile")
    async def file_upload(file: UploadFile = File(...), principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.write", principal, session)
        return (await dependencies.save_uploads([file], principal, session, runtime.upload_root, runtime.settings, runtime.sessions, dependencies.lock_tenant_writes))[0]


    @app.post("/api/File/BatchUploadFile")
    async def file_batch_upload(files: list[UploadFile] = File(...), principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("file.write", principal, session)
        return await dependencies.save_uploads(files, principal, session, runtime.upload_root, runtime.settings, runtime.sessions, dependencies.lock_tenant_writes)


    @app.get("/api/File/UploadLimits")
    async def file_upload_limits(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.write", principal, session)
        return upload_limits(runtime.settings)


    @app.post("/api/File/RemoteUploadFile")
    async def file_remote_upload(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.write", principal, session)
        # 即使旧环境误设开关，也不存在可恢复任意 URL 下载的代码路径。
        raise HTTPException(status_code=403, detail={"code": 403, "errMsg": "远程 URL 导入已关闭，请使用本地上传"})


    @app.get("/api/File/GetById")
    async def file_by_id(Id: str | int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.read", principal, session)
        item = await require_file_access(session, principal, Id)
        return dependencies.serialize_file(item)


    @app.post("/api/File/SetSharing")
    async def file_set_sharing(payload: dict[str, Any], request: Request, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.write", principal, session)
        item = await require_file_access(session, principal, payload.get("id"), write=True)
        if item.owner_id != principal.user_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "文件不存在"})
        mode = payload.get("accessMode")
        if mode not in ("private", "tenant"):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "accessMode 必须为 private 或 tenant"})
        old_mode = file_access_mode(item)
        if mode == "private" and old_mode != "private":
            await dependencies.require_no_shared_file_references(session, item)
        item.data = {**(item.data or {}), "accessMode": mode}
        await dependencies.attach_operation_logs(
            session, tenant_id=principal.tenant_id, user_id=principal.user_id, username=principal.username,
            operate_type="文件共享", operate_remark=f"文件共享范围 {old_mode} -> {mode}", table="files",
            table_id=item.id, payload={"id": item.id, "accessMode": mode}, request=request,
        )
        await session.commit()
        return dependencies.serialize_file(item)


    @app.delete("/api/File/DeleteFile")
    async def file_delete_legacy(id: str | int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        return await file_delete(id, principal, session)


    @app.post("/api/File/GetPageData")
    async def file_page(payload: PageInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("file.read", principal, session)
        criteria = [Resource.tenant_id == principal.tenant_id, Resource.resource_type == "files", Resource.is_deleted.is_(False)]
        if payload.searchKey:
            criteria.append(Resource.name.contains(payload.searchKey))
        criteria.append(readable_file_clause(session, principal.user_id))
        total = await session.scalar(select(func.count()).select_from(Resource).where(*criteria)) or 0
        start = max((payload.pageNum - 1) * payload.pageSize, 0)
        rows = await session.scalars(select(Resource).where(*criteria).order_by(
            Resource.created_at.desc(), Resource.id.desc()).offset(start).limit(payload.pageSize))
        return dependencies.page_result([dependencies.serialize_file(item) for item in rows], payload, total)


    @app.get("/api/File/GetFile")
    @app.get("/api/File/GetFilePath")
    async def file_download(fileid: str | int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> FileResponse:
        await dependencies.ensure_permission("file.read", principal, session)
        item = await require_file_access(session, principal, fileid)
        try:
            source = dependencies.file_source_path(item)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "文件不存在"}) from exc
        return dependencies.FileResponse(source, filename=item.name)


    @app.delete("/api/File/Delete")
    async def file_delete(Id: str | int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("file.write", principal, session)
        item = await require_file_access(session, principal, Id, write=True)
        await dependencies.require_no_shared_file_references(session, item)
        item.is_deleted = True
        await session.commit()
        return True

    return _RouteExports(
        file_upload=file_upload,
        file_batch_upload=file_batch_upload,
        file_upload_limits=file_upload_limits,
        file_remote_upload=file_remote_upload,
        file_by_id=file_by_id,
        file_set_sharing=file_set_sharing,
        file_delete_legacy=file_delete_legacy,
        file_page=file_page,
        file_download=file_download,
        file_delete=file_delete,
    )

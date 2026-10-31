"""frontend 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from fastapi import HTTPException
from fastapi.staticfiles import StaticFiles


@dataclass(frozen=True)
class Dependencies:
    FileResponse: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings):
    def mount_frontend() -> None:
        if not runtime.settings.serve_frontend:
            return
        dist = runtime.settings.frontend_dist_path()
        assets = dist / "assets"
        index_file = dist / "index.html"
        if not index_file.is_file():
            return
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            if full_path.startswith("api/") or full_path in {"health", "docs", "redoc", "openapi.json"}:
                raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "接口不存在"})
            candidate = (dist / full_path).resolve()
            try:
                candidate.relative_to(dist.resolve())
            except ValueError:
                raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "文件不存在"})
            if full_path and candidate.is_file():
                return dependencies.FileResponse(candidate)
            return dependencies.FileResponse(index_file)

    return _RouteExports(
        mount_frontend=mount_frontend,
    )

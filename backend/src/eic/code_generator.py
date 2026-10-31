"""白名单业务实体的离线代码包生成；不读取业务数据、不写项目目录。"""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import Depends, HTTPException, Query, Response
from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class Entity:
    name: str
    slug: str
    description: str
    area: str = "组织业务"
    parent: bool = False


# 只登记无凭据的实体字段；不得反射数据库或接受客户端模板/路径。
ENTITIES = (
    Entity("Department", "department", "部门", parent=True),
    Entity("Position", "position", "岗位"),
)
TEMPLATES = Path(__file__).with_name("codegen_templates")


class BuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entityNames: list[str] = Field(min_length=1, max_length=2)


def catalog() -> list[dict]:
    return [{"entityName": e.name, "description": e.description, "area": e.area,
             "fields": ["id", "name", "description", "active"] + (["parentId"] if e.parent else [])}
            for e in ENTITIES]


def build_archive(names: list[str]) -> bytes:
    allowed = {e.name: e for e in ENTITIES}
    if len(set(names)) != len(names) or any(name not in allowed for name in names):
        raise HTTPException(400, detail={"errMsg": "请选择不重复的已登记业务实体"})
    output = BytesIO()
    manifest = []
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("backend/src/eic/generated/__init__.py", "")
        for name in names:
            entity = allowed[name]
            replacements = {
                "__ENTITY__": entity.name, "__SLUG__": entity.slug, "__LABEL__": entity.description,
                "__PARENT_SCHEMA__": "    parentId: int | None = Field(default=None, ge=1, le=9223372036854775807)" if entity.parent else "",
                "__PARENT_FORM__": '<n-form-item label="上级部门 ID" path="parentId"><n-input v-model:value="form.parentId" placeholder="留空表示顶级部门" /></n-form-item>' if entity.parent else "",
                "__PARENT_DEFAULT__": ", parentId: ''" if entity.parent else "",
                "__PARENT_COPY__": ", parentId: row?.parentId == null ? '' : String(row.parentId)" if entity.parent else "",
                "__PARENT_PAYLOAD__": ", parentId: form.parentId.trim() || null" if entity.parent else "",
            }
            files = {
                f"backend/src/eic/generated/{entity.slug}/schemas.py": "schemas.py.tpl",
                f"backend/src/eic/generated/{entity.slug}/routes.py": "routes.py.tpl",
                f"frontend/src/api/generated/{entity.slug}.js": "api.js.tpl",
                f"frontend/src/pages/generated/{entity.name}Management.vue": "page.vue.tpl",
            }
            for destination, template in files.items():
                source = (TEMPLATES / template).read_text(encoding="utf-8")
                for token, value in replacements.items():
                    source = source.replace(token, value)
                archive.writestr(destination, source)
                manifest.append(destination)
            archive.writestr(f"backend/src/eic/generated/{entity.slug}/__init__.py", "")
        readme = (TEMPLATES / "README.md.tpl").read_text(encoding="utf-8")
        archive.writestr("README.md", readme + "\n## 本次实体\n\n" + "\n".join(names)
                         + "\n\n## 业务文件\n\n" + "\n".join(manifest) + "\n")
    return output.getvalue()


def bind_routes(app, current_principal, get_session, ensure_permission):
    @app.get("/api/CodeGenerator/GetAreaNames")
    async def areas(principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.codegen.read", principal, session)
        return sorted({entity.area for entity in ENTITIES})

    @app.get("/api/CodeGenerator/GetAreaNameEntityItems")
    async def entities(name: str = Query(min_length=1, max_length=40), principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.codegen.read", principal, session)
        if name not in {entity.area for entity in ENTITIES}:
            raise HTTPException(400, detail={"errMsg": "未登记的业务区域"})
        return [entity for entity in catalog() if entity["area"] == name]

    @app.post("/api/CodeGenerator/BulidCode")
    async def build(payload: BuildInput, principal=Depends(current_principal), session=Depends(get_session)):
        await ensure_permission("system.codegen.read", principal, session)
        await ensure_permission("system.codegen.write", principal, session)
        return Response(build_archive(payload.entityNames), media_type="application/zip", headers={
            "Content-Disposition": 'attachment; filename="eic-generated-crud.zip"',
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        })

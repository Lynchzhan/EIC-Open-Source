"""内置模板预览与显式选择导入；所有更新共享租户写锁。"""
from __future__ import annotations

import hashlib
import hmac

from fastapi import HTTPException
from sqlalchemy import select

from eic.ai.prompt_catalog import (
    BY_ID,
    CATALOG,
    CATALOG_VERSION,
    content_digest,
    digest,
    legacy_contents,
    public_template,
)
from eic.core.ids import new_id
from eic.models import AIPrompt, Resource


def fail(message, status=409):
    raise HTTPException(status, detail={"code": status, "errMsg": message})


def validate_fields(payload):
    for key, limit in (("name", 200), ("prompt", 5000), ("description", 500)):
        value = payload.get(key)
        if not isinstance(value, str) or (key != "description" and not value.strip()) or len(value) > limit:
            fail(f"{key} 不能为空或超过长度限制" if key != "description" else "description 格式或长度无效", 422)


def builtin_data(item):
    return {"prompt": item["prompt"], "content": item["prompt"],
            "description": item["description"], "describe": item["description"],
            "source": "eic-builtin", "builtinTemplateId": item["templateId"],
            "builtinVersion": item["version"],
            "builtinDigest": content_digest(item["name"], item["prompt"], item["description"])}


async def rows(session, tenant_id):
    resources = list(await session.scalars(select(Resource).where(
        Resource.tenant_id == tenant_id, Resource.resource_type == "ai_prompts").order_by(Resource.id)))
    prompts = list(await session.scalars(select(AIPrompt).where(
        AIPrompt.tenant_id == tenant_id).order_by(AIPrompt.id)))
    return resources, prompts


def row_state(resource, prompt):
    data = resource.data or {}
    return {"id": str(resource.id), "name": resource.name, "data": data,
            "ownerId": str(resource.owner_id), "isDeleted": resource.is_deleted,
            "updatedAt": resource.updated_at.isoformat() if resource.updated_at else None,
            "dedicated": {"id": str(prompt.id), "name": prompt.name,
                          "content": prompt.content, "enabled": prompt.enabled} if prompt else None}


def preview_entry(item, resources, prompts):
    matches = [r for r in resources if r.name == item["name"]
               or (r.data or {}).get("builtinTemplateId") == item["templateId"]]
    named = [p for p in prompts if p.name == item["name"]]
    base = {**public_template(item), "selectable": False, "action": "skip", "before": None}
    if not matches and not named:
        return {**base, "selectable": True, "action": "create", "status": "missing", "reason": "尚未导入"}
    if len(matches) != 1:
        return {**base, "status": "protected", "reason": "重名或仅存在专用记录，保留现状"}
    resource = matches[0]
    prompt = next((p for p in prompts if p.id == resource.id), None)
    data = resource.data or {}
    current = str(data.get("content") or data.get("prompt") or "")
    description = str(data.get("description") or data.get("describe") or "")
    base["before"] = {"id": str(resource.id), "name": resource.name, "prompt": current, "description": description}
    base["expectedDigest"] = digest(row_state(resource, prompt))
    if data.get("builtinEdited"):
        return {**base, "status": "protected", "reason": "曾由用户编辑，不覆盖"}
    if resource.is_deleted or prompt is None or not prompt.enabled or len(named) != 1 or prompt.name != resource.name:
        return {**base, "status": "protected", "reason": "已删除、停用、重名或双表不完整，保留现状"}
    if data.get("source") == "eic-builtin" and data.get("builtinTemplateId") == item["templateId"]:
        pristine = (data.get("builtinDigest") == content_digest(resource.name, current, description)
                    and prompt.content == current and resource.name == item["name"]
                    and all(data[k] == current for k in ("content", "prompt") if k in data)
                    and all(data[k] == description for k in ("describe", "description") if k in data))
        if pristine and data.get("builtinVersion") == CATALOG_VERSION and current == item["prompt"] and description == item["description"]:
            return {**base, "status": "current", "reason": "当前版本，无需更新"}
        if pristine and data.get("builtinVersion") != CATALOG_VERSION:
            return {**base, "selectable": True, "action": "update", "status": "upgrade", "reason": "未编辑的旧内置版本"}
    legacy = legacy_contents(item)
    if (data.get("source") in {"seed", "public-style-seed"} and resource.name == item["name"]
            and current in legacy and prompt.content in legacy and not description
            and all(data[k] in legacy for k in ("prompt", "content") if k in data)):
        return {**base, "selectable": True, "action": "update", "status": "legacy", "reason": "匹配未编辑的旧官方模板"}
    return {**base, "status": "protected", "reason": "用户自建或已编辑，不覆盖"}


def token_for(principal, revision, secret):
    value = f"{principal.tenant_id}:{principal.user_id}:{CATALOG_VERSION}:{revision}"
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


async def preview(session, principal, secret):
    resources, prompts = await rows(session, principal.tenant_id)
    entries = [preview_entry(item, resources, prompts) for item in CATALOG]
    # 整个预览绑定当前用户/租户与双表快照，前端不可自造或跨租户复用。
    revision = digest({"resources": [row_state(r, next((p for p in prompts if p.id == r.id), None)) for r in resources],
                       "prompts": [{"id": str(p.id), "name": p.name, "content": p.content, "enabled": p.enabled} for p in prompts],
                       "catalog": [public_template(item) for item in CATALOG]})
    return {"catalogVersion": CATALOG_VERSION, "previewToken": token_for(principal, revision, secret), "entries": entries}


async def apply(session, principal, secret, payload):
    selections = payload.get("templateIds")
    if (payload.get("catalogVersion") != CATALOG_VERSION or not isinstance(selections, list)
            or not selections or len(selections) > len(CATALOG)
            or any(not isinstance(k, str) or k not in BY_ID for k in selections)
            or len(set(selections)) != len(selections)):
        fail("请预览当前版本并选择有效模板", 422)
    current = await preview(session, principal, secret)
    supplied = payload.get("previewToken")
    if not isinstance(supplied, str) or not hmac.compare_digest(supplied, current["previewToken"]):
        fail("预览后内容已变化，请重新预览差异")
    entries = {item["templateId"]: item for item in current["entries"]}
    if any(not entries[key]["selectable"] for key in selections):
        fail("所选模板含已编辑或非官方内容，未执行更新")
    changes = []
    for key in selections:
        entry, item = entries[key], BY_ID[key]
        if entry["action"] == "create":
            resource_id = await new_id(session)
            resource = Resource(id=resource_id, tenant_id=principal.tenant_id, resource_type="ai_prompts",
                                name=item["name"], owner_id=principal.user_id, data=builtin_data(item))
            prompt = AIPrompt(id=resource_id, tenant_id=principal.tenant_id, name=item["name"], content=item["prompt"], enabled=True)
            session.add_all([resource, prompt])
        else:
            resource = await session.get(Resource, int(entry["before"]["id"]))
            prompt = await session.get(AIPrompt, resource.id)
            resource.data = {**(resource.data or {}), **builtin_data(item)}
            resource.name, prompt.name, prompt.content = item["name"], item["name"], item["prompt"]
        await session.flush()
        changes.append({"templateId": key, "id": str(resource.id), "action": entry["action"]})
    return {"catalogVersion": CATALOG_VERSION, "changes": changes}


async def ensure_builtin(session, tenant, admin, item):
    """仅空缺初始化：同名、已删除、用户编辑、旧官方模板全部保留。"""
    resources, prompts = await rows(session, tenant.id)
    existing = next((p for p in prompts if p.name == item["name"]), None)
    resource = next((r for r in resources if r.name == item["name"]
                     or (r.data or {}).get("builtinTemplateId") == item["templateId"]), None)
    if existing or resource:
        return existing or resource
    resource_id = await new_id(session)
    prompt = AIPrompt(id=resource_id, tenant_id=tenant.id, name=item["name"], content=item["prompt"], enabled=True)
    session.add_all([prompt, Resource(id=resource_id, tenant_id=tenant.id, resource_type="ai_prompts",
                                    name=item["name"], owner_id=admin.id, data=builtin_data(item))])
    await session.flush()
    return prompt

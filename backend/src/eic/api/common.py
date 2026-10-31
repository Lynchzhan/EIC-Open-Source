"""common 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException
from eic.api.schemas import PageInput as PageInput
from eic.ai.providers import extract_visible_answer
import json
from eic.ai.providers import looks_like_reasoning
from eic.core.ids import supplied_id
from eic.core.reference_ids import coerce_id, first_int, require_id


@dataclass(frozen=True)
class Dependencies:
    coerce_id: Callable[..., Any]
    new_id: Callable[..., Any]
    MODEL_TYPE_FROM_LEGACY: Any
    MODEL_TYPE_TO_LEGACY: Any
    PROVIDER_FROM_LEGACY: Any
    PROVIDER_TO_LEGACY: Any


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    def normalize_model_type(value: Any) -> str:
        if value in (None, ""):
            return "chat"
        if isinstance(value, int):
            return dependencies.MODEL_TYPE_FROM_LEGACY.get(value, "chat")
        key = str(value).strip().lower()
        if key.isdigit():
            return dependencies.MODEL_TYPE_FROM_LEGACY.get(int(key), "chat")
        return dependencies.MODEL_TYPE_FROM_LEGACY.get(key, key or "chat")


    def normalize_provider(value: Any) -> str:
        if value in (None, ""):
            return "openai_compatible"
        if isinstance(value, int):
            return dependencies.PROVIDER_FROM_LEGACY.get(value, str(value))
        key = str(value).strip().lower()
        if key.isdigit():
            return dependencies.PROVIDER_FROM_LEGACY.get(int(key), key)
        return dependencies.PROVIDER_FROM_LEGACY.get(key, key or "openai_compatible")


    def normalize_endpoint(value: str) -> str:
        cleaned = (value or "").strip()
        if cleaned and "://" not in cleaned:
            cleaned = "https://" + cleaned
        return cleaned.rstrip("/")


    def legacy_model_type(value: str) -> int:
        return dependencies.MODEL_TYPE_TO_LEGACY.get((value or "").strip().lower(), 1)


    def sanitize_visible_reply(text: str, user_message: str = "") -> str:
        raw = (text or "").strip()
        if not raw:
            return ""
        visible = extract_visible_answer(raw, user_message)
        if looks_like_reasoning(raw) or "<think>" in raw or "<thinking>" in raw:
            return visible.strip()
        return (visible or raw).strip()


    def legacy_provider(value: str) -> int:
        key = (value or "").strip().lower()
        if key.isdigit():
            return int(key)
        return dependencies.PROVIDER_TO_LEGACY.get(key, 1)


    def page_result(items: list[dict[str, Any]], page: PageInput, total: int) -> dict[str, Any]:
        return {"pageNum": page.pageNum, "pageSize": page.pageSize, "total": total, "data": items}


    def legacy_status(active: bool) -> int:
        return 1 if active else -1


    def parse_active(payload: dict[str, Any], default: bool = True) -> bool:
        if "status" in payload and payload["status"] not in (None, ""):
            try:
                return int(payload["status"]) == 1
            except (TypeError, ValueError):
                return str(payload["status"]).lower() in {"1", "true", "yes"}
        if "active" in payload:
            value = payload["active"]
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return int(value) == 1
            return str(value).lower() in {"1", "true", "yes"}
        return default


    def coerce_id_list(value: Any) -> list[int]:
        if value in (None, "", [], ()):
            return []
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    loaded = json.loads(text)
                    value = loaded if isinstance(loaded, list) else [text]
                except json.JSONDecodeError:
                    value = [part.strip() for part in text.split(",") if part.strip()]
            else:
                value = [part.strip() for part in text.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            ids: list[int] = []
            seen: set[int] = set()
            for item in value:
                item_id = dependencies.coerce_id(item)
                if item_id not in (None, 0) and item_id not in seen:
                    ids.append(item_id)
                    seen.add(item_id)
            return ids
        item_id = dependencies.coerce_id(value)
        return [item_id] if item_id not in (None, 0) else []


    async def new_entity_id(session: AsyncSession, value: int | None = None) -> int:
        if not value:
            return await dependencies.new_id(session)
        try:
            return await supplied_id(session, value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "新对象 ID 无效或尚未由服务端分配"}) from exc


    def skill_tool_type_value(value: Any) -> int | None:
        if value in (None, "", "all", "0", 0):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number in {1, 2, 3} else None

    return _RouteExports(
        normalize_model_type=normalize_model_type,
        normalize_provider=normalize_provider,
        normalize_endpoint=normalize_endpoint,
        legacy_model_type=legacy_model_type,
        sanitize_visible_reply=sanitize_visible_reply,
        legacy_provider=legacy_provider,
        page_result=page_result,
        legacy_status=legacy_status,
        parse_active=parse_active,
        first_int=first_int,
        coerce_id_list=coerce_id_list,
        coerce_id=coerce_id,
        require_id=require_id,
        new_entity_id=new_entity_id,
        skill_tool_type_value=skill_tool_type_value,
    )

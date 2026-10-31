"""聊天、任务与后台摘要共用应用配置读取合同，不依赖 Web 入口。"""
from typing import Any
from eic.core.reference_ids import first_int
from sqlalchemy.ext.asyncio import AsyncSession
from eic.models import AIApp
from eic.core.runtime_refs import RuntimeReferenceUnavailable, require_runtime_resource
from eic.ai.context_policy import normalize_context_config
from eic.ai.response_contract import ResponseContractError, validate_response_schema


def app_response_schema(config: dict[str, Any]) -> dict[str, Any] | None:
    """只消费服务保存的应用契约；普通聊天和工具循环默认关闭结构化响应。"""
    schema = validate_response_schema(config.get("responseSchema"))
    if schema is not None:
        for flag, field in (("isSkill", "skills"), ("isAITools", "tools")):
            if config.get(flag) is True and any(
                isinstance(item, dict) and item.get("isSelect") is True
                for item in (config.get(field) or [])
            ):
                raise ResponseContractError("structured_response_configuration_conflict")
    return schema


CHAT_MODEL_KEYS = ("chatModelID", "chatModelId", "aIModelId", "AIModelId")


def chat_model_id(config):
    return first_int(*(config.get(key) for key in CHAT_MODEL_KEYS))


async def load_application_config(session: AsyncSession, tenant_id: int, app_id: int) -> dict[str, Any]:
    resource, dedicated = await require_runtime_resource(session, tenant_id, int(app_id), "ai_apps", AIApp)
    config = dict(resource.data or {}) if resource is not None else dict(dedicated.config or {})
    config["agentId"] = int(app_id)
    if dedicated is not None:
        for key, value in (("chatModelId", dedicated.chat_model_id), ("promptId", dedicated.prompt_id),
                           ("knowledgeBaseId", dedicated.knowledge_base_id)):
            if value:
                config.setdefault(key, value)
    return config


async def runtime_app_config(session: AsyncSession, tenant_id: int, app_id: int | None) -> dict[str, Any]:
    if not app_id:
        return normalize_context_config({})
    config = await load_application_config(session, tenant_id, app_id)
    try:
        config = normalize_context_config(config)
        app_response_schema(config)
    except (ResponseContractError, ValueError) as exc:
        raise RuntimeReferenceUnavailable(str(exc)) from None
    return config

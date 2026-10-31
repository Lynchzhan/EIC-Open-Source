"""资源写入 DTO；保留扩展字段和缺省语义，权限仍由领域服务校验。"""
from typing import Annotated, Any

from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

Identifier = Annotated[int, Field(strict=True)] | str | None
IdentifierList = list[Identifier] | str | None


class ResourceWrite(BaseModel):
    model_config = ConfigDict(extra='allow', strict=True, hide_input_in_errors=True)

    id: Identifier = None
    name: str | None = None
    title: str | None = None
    description: str | None = None


class AIAppWrite(ResourceWrite):
    chatModelID: Identifier = None
    chatModelId: Identifier = None
    aIModelId: Identifier = None
    AIModelId: Identifier = None
    fallbackModelIds: IdentifierList = None
    lowCostModelIds: IdentifierList = None
    aiPromptIDs: IdentifierList = None
    kmsIds: IdentifierList = None
    skillIds: IdentifierList = None
    runtimePolicy: dict[str, Any] | None = None
    contextConfig: dict[str, Any] | None = None


class KnowledgeWrite(ResourceWrite):
    embeddingModelId: Identifier = None
    embeddingModelID: Identifier = None
    rerankModelId: Identifier = None
    rerankModelID: Identifier = None
    fileIds: IdentifierList = None
    indexingPolicy: dict[str, Any] | None = None


class PromptWrite(ResourceWrite):
    content: str | None = None
    prompt: str | None = None
    describe: str | None = None


class SkillWrite(ResourceWrite):
    fileId: Identifier = None
    toolNames: list[str] | str | None = None


class McpWrite(ResourceWrite):
    endpoint: str | None = None
    url: str | None = None
    headers: dict[str, Any] | None = Field(default=None, repr=False)


class ChatWrite(ResourceWrite):
    appId: Identifier = None
    aIAppsId: Identifier = None


class ChatMessageWrite(ResourceWrite):
    content: str | None = None
    aIChatsId: Identifier = None
    sessionId: Identifier = None
    appId: Identifier = None
    aIAppsId: Identifier = None
    # 附件原始值交给 normalize_chat_files 严格校验，保留既有 HTTP 400 合同。
    fileIds: list[Any] | str | None = None
    memoryWrite: dict[str, Any] | None = None


class MessageWrite(ResourceWrite):
    content: str | None = None


class DictionaryWrite(ResourceWrite):
    type: str | None = None
    value: Any = None


class UserWrite(ResourceWrite):
    nickName: str | None = None
    email: str | None = None
    phone: str | None = None
    departmentId: Identifier = None
    positionId: Identifier = None
    roleIds: IdentifierList = None
    passWord: str | None = Field(default=None, repr=False)
    password: str | None = Field(default=None, repr=False)


class RoleWrite(ResourceWrite):
    remarks: str | None = None


class DepartmentWrite(ResourceWrite):
    parentId: Identifier = None


class PositionWrite(ResourceWrite):
    pass


class TenantWrite(ResourceWrite):
    code: Identifier = None


class PermissionWrite(ResourceWrite):
    roleId: Identifier = None


RESOURCE_INPUTS: dict[str, type[ResourceWrite]] = {
    'ai_apps': AIAppWrite,
    'knowledge_bases': KnowledgeWrite,
    'ai_prompts': PromptWrite,
    'skills': SkillWrite,
    'mcp_configs': McpWrite,
    'chats': ChatWrite,
    'chat_messages': ChatMessageWrite,
    'messages': MessageWrite,
    'dictionaries': DictionaryWrite,
}


def write_payload(payload: ResourceWrite | dict[str, Any], schema: type[ResourceWrite]) -> dict[str, Any]:
    # 内部调用与 HTTP 路由使用同一合同；不插入未提供的默认值，避免部分更新覆盖旧数据。
    try:
        data = payload.model_dump(exclude_unset=True) if isinstance(payload, ResourceWrite) else payload
        parsed = schema.model_validate(data)
    except ValidationError as exc:
        # 只带定位和错误类型，避免错误对象携带模型密钥或业务输入正文。
        errors = [{'loc': ('body', *error['loc']), 'msg': error['msg'], 'type': error['type']}
                  for error in exc.errors(include_input=False, include_context=False, include_url=False)]
        raise RequestValidationError(errors) from None
    return parsed.model_dump(exclude_unset=True)


def resource_payload(resource_type: str, payload: ResourceWrite | dict[str, Any]) -> dict[str, Any]:
    return write_payload(payload, RESOURCE_INPUTS.get(resource_type, ResourceWrite))

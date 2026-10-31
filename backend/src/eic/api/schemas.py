"""HTTP 输入 DTO，保持既有字段别名与校验约束。"""
from datetime import datetime
from typing import Any, Literal
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

class LoginInput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    name: str
    password: str = Field(alias="passWord")
    tenant_id: int = Field(default=1000, alias="tenantId")

class RegisterInput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    name: str
    password: str = Field(alias="passWord")
    tenant_id: int = Field(default=1000, alias="tenantId")
    nickname: str = Field(default="", alias="nickName")
    phone: str = ""
    email: str = ""
    department_id: int | None = Field(default=None, alias="departmentId")
    position_id: int | None = Field(default=None, alias="positionId")
    registration_code: str = Field(default="", alias="registrationCode", max_length=128)

class PageInput(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    pageNum: int = Field(default=1, validation_alias=AliasChoices("pageNum", "page"))
    pageSize: int = Field(default=20, validation_alias=AliasChoices("pageSize", "limit"))
    searchKey: str = Field(default="", validation_alias=AliasChoices("searchKey", "search"))
    parameter: Any = Field(default=None)

class AIModelInput(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)
    id: int | str | None = None
    provider: int | str = Field(default=1, alias="aiType")
    model_type: int | str = Field(default=1, alias="aiModelType")
    endpoint: str = Field(default="", alias="endPoint")
    model_name: str = Field(default="", alias="modelName")
    description: str = Field(default="", alias="modelDescription")
    embedding_dimensions: int | None = Field(default=None, alias="embeddingValueSize")
    max_input_tokens: int = Field(default=131072, alias="maxAskPromptSize", strict=True, ge=1, le=2_000_000)
    max_output_tokens: int = Field(default=8192, alias="answerTokens", strict=True, ge=1, le=2_000_000)
    chat_parameter_profile: Literal["auto", "openai_legacy", "openai_completion", "openai_reasoning", "ollama"] = Field(default="auto", alias="chatParameterProfile")
    reasoning_policy: dict | None = Field(default=None, alias="reasoningPolicy")
    search_mode: Literal['disabled', 'independent', 'native'] = Field(default='disabled', alias='searchMode')
    api_key: str | None = Field(default=None, validation_alias=AliasChoices("apiKey", "modelKey", "ModelKey"), serialization_alias="apiKey", repr=False)
    restore_reference_ids: list[str] = Field(default_factory=list, alias="restoreReferenceIds")
    set_default: bool = Field(default=False, alias="setDefault")

class AIModelTestInput(AIModelInput):
    config_source: Literal["saved", "form"] | None = Field(default=None, alias="configSource")
    expected_preferences_revision: int | None = Field(default=None, alias="expectedPreferencesRevision", strict=True, ge=0)

class ResourceInput(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: int | None = None
    name: str = ""

class TaskInput(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    id: int | None = None
    name: str = Field(min_length=1, max_length=200)
    cron: str = "0 * * * *"
    task_type: Literal["cron", "once"] = Field(default="cron", alias="taskType")
    run_at: datetime | None = Field(default=None, alias="runAt")
    max_retries: int = Field(default=3, alias="maxRetries", ge=1, le=10)
    payload: dict[str, Any] = Field(default_factory=dict)

class BuiltinSkillInstall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keys: list[str] = Field(min_length=1, max_length=3)

class SkillExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skillId: int = Field(gt=0)
    appId: int | None = Field(default=None, gt=0)
    arguments: dict[str, Any]
    idempotencyKey: str = Field(min_length=8, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

class SkillEnabledInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skillId: int = Field(gt=0)
    enabled: bool = Field(strict=True)

class ApprovalTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requestId: str = Field(min_length=1, max_length=80)
    runId: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    callId: str = Field(min_length=1, max_length=120)
    inputDigest: str = Field(pattern=r"^[a-f0-9]{64}$")
    checkpointSeq: int = Field(ge=1)
    idempotencyKey: str = Field(min_length=8, max_length=120)

class ApprovalDecision(ApprovalTicket):
    decision: Literal["approve", "reject"]

# Copyright (c) 2026 Le7
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import anyio
import asyncpg
import jwt
import redis.asyncio as redis_async
from cryptography.fernet import InvalidToken
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from redis.exceptions import RedisError
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from eic.ai import attachments as chat_attachments
from eic.ai import search as chat_search
from eic.ai.context_policy import context_settings, memory_settings, normalize_context_config
from eic.ai import conversation_context, context_access
from eic import code_generator
from eic import model_lifecycle
from eic import experience
from eic.ai.memory_context import memory_message, memory_snapshot_valid, recall_memory
from eic.ai.context_manifest import bounded_tool_result, knowledge_evidence_content, prepare_context, working_manifest
from eic.ai.response_contract import ResponseContractError, parse_response, response_format, validate_response_schema
from eic.ai.reliable_calls import reliable_stream, reliable_tool, validate_tool_arguments
from eic.ai.local_fallback import authorized_excerpt
from eic.ai.shared_executor import (
    ExecutionServices, ExecutorCheckpointer, build_execution_graph, stream_shared,
)
from eic.ai.agent_graph import (
    AGENT_GRAPH_VERSION, CHAT_AGENT_GRAPH as CHAT_AGENT_GRAPH, AgentBudget, ToolCallRequest, normalize_model_decision,
    plan_chat_execution,
)
from eic.ai.budget import (
    ChatConfigurationError,
    app_temperature,
    budget_conversation as budget_conversation,
    message_units,
    text_units,
)
from eic.ai.mcp_client import HttpMcpClient, McpClientError
from eic.ai.standard_tools import ControlledToolSet, StandardToolDefinition
from eic.ai import chat_mcp_tools
from eic.ai import declarative_skills
from eic.ai.checkpointer import DatabaseCheckpointer as DatabaseCheckpointer
from eic.ai.checkpoint_security import CheckpointReadScope, sanitize_tool_result
from eic.core.tool_intent import (
    ToolIntentConflict, ToolIntentInProgress, begin_tool_intent, finish_tool_intent,
)
from eic.ai.providers import (
    ModelProviderError,
    chat_parameters,
    extract_visible_answer,
    looks_like_reasoning,
    probe_model_connection as probe_model_connection,
    stream_chat_completion,
)
from eic.ai.skill_manager import SkillManager, validate_skill_archive
from eic.ai.builtin_skills import (
    CATALOG as BUILTIN_SKILLS, builtin_data, register_builtin_tools, resource_enabled, trusted_builtin,
)
from eic.ai.stream_reader import complete_cleanup, stream_with_heartbeats
from eic.ai.tool_center import ToolApprovalGrant, ToolCenter, ToolDefinition, ToolExecutionError
from eic.core import agent_approval, agent_runtime, agent_worker, generation, outbox, task_schedule as task_schedule
from eic.core.chat_tool_cleanup import converge_child_unknown
from eic.core import observability
from eic.core import memory_lifecycle
from eic.core.langfuse_context import prompt_identity, turn_context
from eic.core import observability_query
from eic.core import registration_codes
from eic.core import queue as task_queue
from eic.core import tasks
task_contract = tasks
from eic.core.config import get_settings
from eic.core.database import SessionLocal, engine, get_session
from eic.core.file_access import (
    can_access_file,
    file_access_mode,
    file_source_path,
    require_file_access,
    valid_file_id,
)
from eic.core.ids import new_id, supplied_id
from eic.core.message_reads import read_fields, receipts
from eic.core.organization import cycle_members, validate_parent
from eic.core.queue import close_queue
from eic.core.runtime_refs import (
    RuntimeReferenceUnavailable,
    require_runtime_model,
    require_runtime_resource,
)
from eic.core.schema import require_schema
from eic.core.security import (
    credential_stamp,
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    hash_password,
    secret_hint as secret_hint,
    verify_and_upgrade_password as verify_and_upgrade_password,
    verify_password,
)
from eic.core.uploads import UploadBodyLimit, save_uploads
from eic.core.authentication import Principal, resolve_identity, revoke_browser_session as revoke_browser_session, session_active
from eic.core.uploads import limits as upload_limits
from eic.knowledge.api_bind import bind_knowledge_extra_routes
from eic.knowledge.ops import (
    apply_knowledge_payload,
    retrieval_public_fields,
    validate_knowledge_model_bindings,
)
from eic.knowledge.langchain_entrypoint import retrieve_knowledge
from eic.models import (
    AgentMemory,
    AgentRun,
    AgentEvent,
    AgentToolCall,
    AIApp,
    AIModel,
    AIPrompt,
    ChatEvent,
    ChatMessage,
    ChatSession,
    Department,
    GenerationLease,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    MessageReadReceipt,
    OutboxEvent,
    Permission,
    Position,
    Resource,
    Role,
    RolePermission,
    ScheduledTask as ScheduledTask,
    TaskRun as TaskRun,
    Tenant,
    ToolAudit,
    User,
    UserRole,
)
from eic.seed import seed_demo_data
from eic.ai import prompt_templates
from eic.ai.prompt_catalog import CATALOG, CATALOG_VERSION, public_template

settings = get_settings()
UPLOAD_ROOT = settings.runtime_path(settings.upload_root)
SKILL_ROOT = settings.runtime_path(settings.skill_root)
UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
skill_manager = SkillManager()
tool_center = ToolCenter()
tool_center.register(
    ToolDefinition(
        name="calculator",
        description="执行四则运算",
        handler=lambda args: {"value": float(args["a"]) + float(args["b"])},
        input_schema={
            "type": "object",
            "required": ["a", "b"],
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "additionalProperties": False,
        },
        timeout_ms=5_000,
    )
)
register_builtin_tools(tool_center)

from eic.api.runtime import RuntimeBindings
_runtime = RuntimeBindings(
    settings_provider=lambda: settings, sessions_provider=lambda: SessionLocal,
    engine_provider=lambda: engine, tools_provider=lambda: tool_center,
    skills_provider=lambda: skill_manager, upload_root_provider=lambda: UPLOAD_ROOT,
    skill_root_provider=lambda: SKILL_ROOT, graph_provider=lambda: CHAT_AGENT_GRAPH,
)


from eic.api.schemas import AIModelInput as AIModelInput
from eic.api.schemas import AIModelTestInput as AIModelTestInput
from eic.api.schemas import ApprovalDecision as ApprovalDecision
from eic.api.schemas import ApprovalTicket as ApprovalTicket
from eic.api.schemas import BuiltinSkillInstall as BuiltinSkillInstall
from eic.api.schemas import LoginInput as LoginInput
from eic.api.schemas import PageInput as PageInput
from eic.api.schemas import RegisterInput as RegisterInput
from eic.api.schemas import ResourceInput as ResourceInput
from eic.api.schemas import SkillEnabledInput as SkillEnabledInput
from eic.api.schemas import SkillExecutionInput as SkillExecutionInput
from eic.api.schemas import TaskInput as TaskInput


MODEL_TYPE_FROM_LEGACY = {
    1: "chat", 2: "embedding", 4: "rerank",
    "1": "chat", "2": "embedding", "4": "rerank",
    "chat": "chat", "embedding": "embedding", "rerank": "rerank",
}
MODEL_TYPE_TO_LEGACY = {"chat": 1, "embedding": 2, "rerank": 4}
PROVIDER_FROM_LEGACY = {
    1: "openai_compatible", 2: "azure", 3: "zhipu", 7: "bge_embedding",
    8: "bge_rerank", 10: "ollama", 11: "ollama_embedding", 12: "dashscope",
    "1": "openai_compatible", "2": "azure", "3": "zhipu", "7": "bge_embedding",
    "8": "bge_rerank", "10": "ollama", "11": "ollama_embedding", "12": "dashscope",
    "openai": "openai_compatible", "openai_compatible": "openai_compatible",
    "azure": "azure", "zhipu": "zhipu", "ollama": "ollama",
    "ollamaembedding": "ollama_embedding", "dashscope": "dashscope",
}
PROVIDER_TO_LEGACY = {
    "openai_compatible": 1, "azure": 2, "zhipu": 3, "bge_embedding": 7,
    "bge_rerank": 8, "ollama": 10, "ollama_embedding": 11, "dashscope": 12,
    "dashscope_rerank": 12, "10": 10,
}


from eic.api.common import bind_routes as bind_api_common, Dependencies as api_common_Dependencies
_api_common = bind_api_common(
    dependencies=api_common_Dependencies(
        coerce_id=lambda *args, **kwargs: coerce_id(*args, **kwargs),
        new_id=new_id,
        MODEL_TYPE_FROM_LEGACY=MODEL_TYPE_FROM_LEGACY,
        MODEL_TYPE_TO_LEGACY=MODEL_TYPE_TO_LEGACY,
        PROVIDER_FROM_LEGACY=PROVIDER_FROM_LEGACY,
        PROVIDER_TO_LEGACY=PROVIDER_TO_LEGACY,
    ), runtime=_runtime,
)
normalize_model_type = _api_common.normalize_model_type
normalize_provider = _api_common.normalize_provider
normalize_endpoint = _api_common.normalize_endpoint
legacy_model_type = _api_common.legacy_model_type
sanitize_visible_reply = _api_common.sanitize_visible_reply
legacy_provider = _api_common.legacy_provider
page_result = _api_common.page_result
legacy_status = _api_common.legacy_status
parse_active = _api_common.parse_active
first_int = _api_common.first_int
coerce_id_list = _api_common.coerce_id_list
coerce_id = _api_common.coerce_id
require_id = _api_common.require_id
new_entity_id = _api_common.new_entity_id
skill_tool_type_value = _api_common.skill_tool_type_value


CHINESE_REPLY_INSTRUCTION = (
    "【输出语言】始终使用简体中文撰写对用户可见的回答正文。"
    "不要用英文段落作答；专有名词、模型名、代码、API 字段和引用原文可保留。"
    "若思考过程夹杂英文，最终回答仍必须是中文。"
)


async def current_principal(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
    request: Request = None,
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail={"code": 401, "errMsg": "请先登录"})
    try:
        payload = decode_access_token(authorization.split(" ", 1)[1])
        principal = await resolve_identity(payload, session)
    except (jwt.PyJWTError, KeyError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail={"code": 401, "errMsg": "登录已过期"}) from exc
    context = request.scope.get("state", {}).get("observation_context") if request is not None else None
    if context is not None:
        context.update(tenant_id=principal.tenant_id, owner_key=observability.actor_key(principal.tenant_id, principal.user_id))
    return principal


from eic.core.authorization import expand_menu_codes, permission_codes, has_permission, ensure_permission


PERMISSION_DEFINITIONS = (
    ("system.codegen.read", "查看代码生成目录", "system"), ("system.codegen.write", "下载业务代码包", "system"),
    ("system.user.read", "查看用户", "system"), ("system.user.write", "管理用户", "system"),
    ("system.role.read", "查看角色", "system"), ("system.role.write", "管理角色", "system"),
    ("system.permission.read", "查看权限", "system"), ("system.permission.write", "管理权限", "system"),
    ("system.tenant.read", "查看租户", "system"), ("system.tenant.write", "管理租户", "system"),
    ("system.department.read", "查看部门", "system"), ("system.department.write", "管理部门", "system"),
    ("system.position.read", "查看岗位", "system"), ("system.position.write", "管理岗位", "system"),
    ("ai.model.read", "查看模型", "ai"), ("ai.model.write", "管理模型", "ai"),
    ("ai.resource.read", "查看 AI 资源", "ai"), ("ai.resource.write", "管理 AI 资源", "ai"),
    ("ai.chat.read", "查看聊天", "ai"), ("ai.chat.write", "使用聊天", "ai"),
    ("knowledge.read", "查看知识库", "knowledge"), ("knowledge.write", "管理知识库", "knowledge"),
    ("skill.read", "查看技能", "ai"), ("skill.write", "管理技能", "ai"),
    ("mcp.read", "查看 MCP", "ai"), ("mcp.write", "管理 MCP", "ai"), ("mcp.call", "调用 MCP", "ai"),
    ("tool.execute", "执行工具", "ai"), ("memory.write", "写入记忆", "ai"),
    ("task.read", "查看任务", "system"), ("task.write", "管理任务", "system"),
    ("file.read", "读取文件", "system"), ("file.write", "上传文件", "system"),
)

MENU_PERMISSION_DEFINITIONS = (
    ("Menu/SystemSettings/CodeGenerator", "代码生成器", "menu"),
    ("Menu/Home", "首页", "menu"), ("Menu/Home/Index", "首页仪表盘", "menu"),
    ("Menu/MyMenu", "我的", "menu"), ("Menu/MyMenu/MyMessage", "我的消息", "menu"),
    ("Menu/MyMenu/MyAiChat", "我的AI对话", "menu"), ("Menu/MyMenu/MyAiTasks", "我的AI自动任务", "menu"),
    ("Menu/MyMenu/MyAiAgents", "我的可用智能体", "menu"),
    ("Menu/AIManagement", "AI管理", "menu"), ("Menu/AIManagement/AIAppsManagement", "智能体管理", "menu"),
    ("Menu/AIManagement/AIPromptsManagement", "提示词管理", "menu"),
    ("Menu/AIManagement/AIKmssManagement", "知识库管理", "menu"),
    ("Menu/AIManagement/AIModelManagement", "模型配置管理", "menu"),
    ("Menu/AIManagement/AISkillToolManagement", "AI技能工具管理", "menu"),
    ("Menu/AIManagement/AIRecallTest", "召回测试", "menu"),
    ("Menu/OrganizationalManagement", "组织架构", "menu"),
    ("Menu/OrganizationalManagement/PositionManagement", "岗位管理", "menu"),
    ("Menu/OrganizationalManagement/DepartmentManagement", "部门管理", "menu"),
    ("Menu/UserManagement", "用户管理", "menu"), ("Menu/UserManagement/UserList", "用户列表", "menu"),
    ("Menu/UserManagement/UserRole", "角色管理", "menu"), ("Menu/UserManagement/UserPermission", "权限管理", "menu"),
    ("Menu/SystemSettings", "系统管理", "menu"), ("Menu/SystemSettings/SystemAnnouncement", "系统公告", "menu"),
    ("Menu/SystemSettings/SystemDic", "系统配置", "menu"), ("Menu/SystemSettings/SystemTenant", "租户管理", "menu"),
    ("Menu/SystemSettings/LogManagement", "日志管理", "menu"), ("Menu/SystemSettings/OsLog", "关键数据变动日志", "menu"),
)

ALL_PERMISSION_DEFINITIONS = PERMISSION_DEFINITIONS + MENU_PERMISSION_DEFINITIONS
ALL_PERMISSION_CODES = tuple(code for code, _, _ in ALL_PERMISSION_DEFINITIONS)
STANDARD_USER_PERMISSION_CODES = (
    "Menu/Home", "Menu/Home/Index",
    "Menu/MyMenu", "Menu/MyMenu/MyMessage", "Menu/MyMenu/MyAiChat", "Menu/MyMenu/MyAiTasks", "Menu/MyMenu/MyAiAgents",
    "ai.chat.read", "ai.chat.write", "ai.resource.read",
    "task.read", "task.write", "file.read", "file.write",
)


@asynccontextmanager
async def bootstrap_session() -> AsyncIterator[AsyncSession]:
    """Serialize first-run bootstrap across multiple MySQL web workers."""
    async with SessionLocal() as session:
        locked = False
        if settings.database_url.startswith("mysql"):
            acquired = await session.scalar(text("SELECT GET_LOCK(:lock_name, 30)"), {"lock_name": "eic-bootstrap"})
            if acquired != 1:
                raise RuntimeError("could not acquire the bootstrap lock")
            locked = True
        try:
            yield session
        finally:
            if locked:
                await session.execute(text("SELECT RELEASE_LOCK(:lock_name)"), {"lock_name": "eic-bootstrap"})


async def bootstrap_empty_environment() -> None:
    """Create an explicit initial admin only when an environment secret is supplied."""
    if not settings.bootstrap_enabled:
        return
    async with bootstrap_session() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.code == 1000))
        existing_users = await session.scalar(select(func.count()).select_from(User)) or 0
        if tenant is None and (settings.bootstrap_admin_password or existing_users):
            tenant = Tenant(id=await new_id(session), code=1000, name="默认租户")
            session.add(tenant)
            await session.flush()
        if tenant is None:
            return
        admin = await session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == settings.bootstrap_admin))
        if admin is None and settings.bootstrap_admin_password and not existing_users:
            admin = User(
                id=await new_id(session), tenant_id=tenant.id, username=settings.bootstrap_admin,
                nickname="系统管理员", password_hash=hash_password(settings.bootstrap_admin_password),
                is_super_admin=True,
            )
            session.add(admin)
            await session.flush()
        role = await session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "超级管理员"))
        if role is None and admin is not None:
            role = Role(id=await new_id(session), tenant_id=tenant.id, name="超级管理员", description="系统初始化角色")
            session.add(role)
            await session.flush()
        existing_permission_ids = set(await session.scalars(select(Permission.id)))
        for code, name, category in ALL_PERMISSION_DEFINITIONS:
            if code not in existing_permission_ids:
                session.add(Permission(id=code, code=code, name=name, category=category))
        if admin is not None and role is not None:
            has_role = await session.scalar(select(UserRole).where(UserRole.user_id == admin.id, UserRole.role_id == role.id))
            if has_role is None:
                session.add(UserRole(user_id=admin.id, role_id=role.id))
            assigned = set(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
            session.add_all(RolePermission(role_id=role.id, permission_id=code) for code, _, _ in ALL_PERMISSION_DEFINITIONS if code not in assigned)
        standard_role = await session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "普通用户"))
        if standard_role is None:
            standard_role = Role(id=await new_id(session), tenant_id=tenant.id, name="普通用户", description="注册用户默认角色，可使用首页、对话和自动任务")
            session.add(standard_role)
            await session.flush()
        assigned = set(await session.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == standard_role.id)))
        session.add_all(RolePermission(role_id=standard_role.id, permission_id=code) for code in STANDARD_USER_PERMISSION_CODES if code not in assigned)
        if standard_role is not None:
            users = await session.scalars(select(User).where(User.tenant_id == tenant.id, User.is_super_admin.is_(False), User.is_active.is_(True)))
            for item in users:
                has_any = await session.scalar(select(UserRole).where(UserRole.user_id == item.id))
                if has_any is None:
                    session.add(UserRole(user_id=item.id, role_id=standard_role.id))
                if not getattr(item, "approval_status", None):
                    item.approval_status = "approved"
        # 历史无 owner 任务由独立回填方案处理，不在启动时自动归属管理员。
        await session.commit()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from eic.core.runtime_io import start_runtime_io, stop_runtime_io
    settings.validate_production_security()
    io_started = False
    recovery_task = None
    outbox_task = None
    experience_task = None
    try:
        if settings.auto_create_schema or settings.runtime_schema_updates:
            raise RuntimeError("Startup DDL switches are retired; run the isolated release migration step")
        await require_schema(engine)
        start_runtime_io()
        io_started = True
        experience_task = asyncio.create_task(experience.cleanup_loop(), name="eic-experience-cleanup")
        await observability.collector.start()
        UPLOAD_ROOT.mkdir(exist_ok=True)
        if settings.bootstrap_enabled:
            await bootstrap_empty_environment()
        if settings.seed_demo_data:
            await seed_demo_data()
        if getattr(settings, "generation_recovery_enabled", False):
            recovery_task = asyncio.create_task(generation.recovery_loop(), name="eic-generation-recovery")
        if getattr(settings, "outbox_dispatch_enabled", False):
            outbox_task = asyncio.create_task(outbox.dispatch_loop(), name="eic-outbox-dispatch")
        yield
    finally:
        for task in list(experience.RUNNING.values()):
            task.cancel()
        if experience_task is not None:
            experience_task.cancel()
            await asyncio.gather(experience_task, *list(experience.RUNNING.values()), return_exceptions=True)
        try:
            await generation.stop_producers()
            if recovery_task is not None:
                recovery_task.cancel()
                await asyncio.gather(recovery_task, return_exceptions=True)
            if outbox_task is not None:
                outbox_task.cancel()
                await asyncio.gather(outbox_task, return_exceptions=True)
        finally:
            try:
                await observability.collector.stop()
            finally:
                try:
                    await close_queue()
                finally:
                    try:
                        if io_started:
                            await stop_runtime_io()
                    finally:
                        await engine.dispose()


JS_MAX_SAFE_INTEGER = 2**53 - 1


def stringify_unsafe_ints(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: stringify_unsafe_ints(item) for key, item in value.items()}
    if isinstance(value, list):
        return [stringify_unsafe_ints(item) for item in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int) and abs(value) > JS_MAX_SAFE_INTEGER:
        return str(value)
    return value


class SafeJSONResponse(JSONResponse):
    def render(self, content: Any) -> bytes:
        return json.dumps(
            stringify_unsafe_ints(content),
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
        ).encode("utf-8")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan, default_response_class=SafeJSONResponse)
app.add_middleware(observability.ObservationMiddleware)
app.add_middleware(UploadBodyLimit, settings=settings)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    id_related = any(
        str(err.get("type", "")).endswith(("int_parsing", "int_type"))
        and any(str(part).lower() in {"id", "fileid"} for part in err.get("loc", ()))
        for err in errors
    )
    message = "文件 ID 无效" if id_related else str((errors[0] if errors else {}).get("msg") or "参数无效")
    payload = {"code": 422, "errMsg": message}
    return JSONResponse(status_code=422, content={"detail": payload, **payload})


AREA_LABELS = {"system": "系统", "ai": "AI", "knowledge": "知识库", "menu": "菜单"}
MODULE_LABELS = {
    "user": "用户", "role": "角色", "permission": "权限", "tenant": "租户",
    "department": "部门", "position": "岗位", "model": "模型", "resource": "AI资源",
    "chat": "对话", "knowledge": "知识库", "skill": "技能", "mcp": "MCP",
    "tool": "工具", "memory": "记忆", "task": "任务", "file": "文件",
    "Home": "首页", "MyMenu": "我的", "AIManagement": "AI管理",
    "OrganizationalManagement": "组织架构", "UserManagement": "用户管理", "SystemSettings": "系统管理",
}

from eic.api.organization import bind_routes as bind_api_organization, Dependencies as api_organization_Dependencies
_api_organization = bind_api_organization(app, 
    dependencies=api_organization_Dependencies(
        ensure_permission=ensure_permission,
        first_int=first_int,
        has_permission=has_permission,
        legacy_status=legacy_status,
        lock_tenant_writes=lambda *args, **kwargs: lock_tenant_writes(*args, **kwargs),
        new_entity_id=new_entity_id,
        new_id=new_id,
        page_result=page_result,
        parse_active=parse_active,
        permission_codes=permission_codes,
        require_id=require_id,
        ALL_PERMISSION_DEFINITIONS=ALL_PERMISSION_DEFINITIONS,
        AREA_LABELS=AREA_LABELS,
        MODULE_LABELS=MODULE_LABELS,
    ), runtime=_runtime,
    current_principal=current_principal,
)
serialize_department = _api_organization.serialize_department
serialize_position = _api_organization.serialize_position
serialize_tenant = _api_organization.serialize_tenant
serialize_role = _api_organization.serialize_role
permission_module_key = _api_organization.permission_module_key
serialize_permission = _api_organization.serialize_permission
serialize_area_permissions = _api_organization.serialize_area_permissions
create_registration_code = _api_organization.create_registration_code
list_registration_codes = _api_organization.list_registration_codes
revoke_registration_code = _api_organization.revoke_registration_code
serialize_user = _api_organization.serialize_user
validate_organization_reference = _api_organization.validate_organization_reference
persist_user_profile = _api_organization.persist_user_profile
get_user = _api_organization.get_user
get_snowflake_id = _api_organization.get_snowflake_id
get_guid = _api_organization.get_guid
get_user_permissions = _api_organization.get_user_permissions
user_page = _api_organization.user_page
user_add_edit = _api_organization.user_add_edit
user_delete = _api_organization.user_delete
user_delete_legacy = _api_organization.user_delete_legacy
user_role_key = _api_organization.user_role_key
user_count = _api_organization.user_count
pending_approvals = _api_organization.pending_approvals
approve_registration = _api_organization.approve_registration
reject_registration = _api_organization.reject_registration
role_page = _api_organization.role_page
role_add_edit = _api_organization.role_add_edit
role_delete = _api_organization.role_delete
role_delete_legacy = _api_organization.role_delete_legacy
role_details = _api_organization.role_details
role_all = _api_organization.role_all
permission_details = _api_organization.permission_details
permission_for_role = _api_organization.permission_for_role
permission_page = _api_organization.permission_page
permission_assign = _api_organization.permission_assign
permission_edit = _api_organization.permission_edit
permission_delete = _api_organization.permission_delete
permission_reload = _api_organization.permission_reload
permission_list = _api_organization.permission_list
department_list = _api_organization.department_list
position_list = _api_organization.position_list
require_super_admin = _api_organization.require_super_admin
department_page = _api_organization.department_page
department_add_edit = _api_organization.department_add_edit
department_delete = _api_organization.department_delete
department_tree = _api_organization.department_tree
position_page = _api_organization.position_page
position_add_edit = _api_organization.position_add_edit
position_delete = _api_organization.position_delete
position_tree = _api_organization.position_tree
tenant_page = _api_organization.tenant_page
tenant_add_edit = _api_organization.tenant_add_edit
tenant_delete = _api_organization.tenant_delete
change_password = _api_organization.change_password


from eic.api.knowledge import bind_routes as bind_api_knowledge, Dependencies as api_knowledge_Dependencies
_api_knowledge = bind_api_knowledge(app, 
    dependencies=api_knowledge_Dependencies(
        all_resources=lambda *args, **kwargs: all_resources(*args, **kwargs),
        apply_knowledge_payload=lambda *args, **kwargs: apply_knowledge_payload(*args, **kwargs),
        attach_operation_logs=lambda *args, **kwargs: attach_operation_logs(*args, **kwargs),
        coerce_id=coerce_id,
        delete_resource=lambda *args, **kwargs: delete_resource(*args, **kwargs),
        enrich_resource_items=lambda *args, **kwargs: enrich_resource_items(*args, **kwargs),
        ensure_permission=lambda *args, **kwargs: ensure_permission(*args, **kwargs),
        has_permission=has_permission,
        lock_tenant_writes=lambda *args, **kwargs: lock_tenant_writes(*args, **kwargs),
        new_id=new_id,
        require_id=require_id,
        resource_page=lambda *args, **kwargs: resource_page(*args, **kwargs),
        retrieve_knowledge=lambda *args, **kwargs: retrieve_knowledge(*args, **kwargs),
        save_resource=lambda *args, **kwargs: save_resource(*args, **kwargs),
        serialize_resource=lambda *args, **kwargs: serialize_resource(*args, **kwargs),
        validate_shared_file_payload=lambda *args, **kwargs: validate_shared_file_payload(*args, **kwargs),
    ), runtime=_runtime,
    current_principal=current_principal,
)
queue_or_run_document = _api_knowledge.queue_or_run_document
knowledge_base_add_edit = _api_knowledge.knowledge_base_add_edit
knowledge_base_delete = _api_knowledge.knowledge_base_delete
ai_apps_list = _api_knowledge.ai_apps_list
ai_prompts_list = _api_knowledge.ai_prompts_list
prompt_builtin_catalog = _api_knowledge.prompt_builtin_catalog
prompt_builtin_preview = _api_knowledge.prompt_builtin_preview
prompt_builtin_apply = _api_knowledge.prompt_builtin_apply
knowledge_bases_all_list = _api_knowledge.knowledge_bases_all_list
ai_app_new_initialization = _api_knowledge.ai_app_new_initialization
resource_details = _api_knowledge.resource_details
knowledge_list = _api_knowledge.knowledge_list
knowledge_queue_index = _api_knowledge.knowledge_queue_index
knowledge_import_file = _api_knowledge.knowledge_import_file
knowledge_retrieval_trace = _api_knowledge.knowledge_retrieval_trace


SENSITIVE_LOG_KEYS = {
    "password", "passWord", "PassWord", "oldPassword", "oldPassWord", "OldPwd",
    "newPassword", "NewPwd", "confirmPassword", "apiKey", "secretKey", "SecretKey",
    "modelKey", "ModelKey", "token", "authorization", "registrationCode", "registration_code",
    "accessToken", "refreshToken", "clientSecret", "workerToken",
}


from eic.api.audit import bind_routes as bind_api_audit, Dependencies as api_audit_Dependencies
_api_audit = bind_api_audit(
    dependencies=api_audit_Dependencies(
        new_id=new_id,
        SENSITIVE_LOG_KEYS=SENSITIVE_LOG_KEYS,
    ), runtime=_runtime,
)
sensitive_log_key = _api_audit.sensitive_log_key
redact_for_log = _api_audit.redact_for_log
request_ip = _api_audit.request_ip
request_device = _api_audit.request_device
attach_operation_logs = _api_audit.attach_operation_logs


from eic.api.resources import bind_routes as bind_api_resources, Dependencies as api_resources_Dependencies
_api_resources = bind_api_resources(app, 
    dependencies=api_resources_Dependencies(
        app_response_schema=lambda *args, **kwargs: app_response_schema(*args, **kwargs),
        attach_operation_logs=attach_operation_logs,
        coerce_id=coerce_id,
        coerce_id_list=coerce_id_list,
        ensure_permission=ensure_permission,
        file_source_path=file_source_path,
        first_int=first_int,
        new_entity_id=new_entity_id,
        new_id=new_id,
        page_result=page_result,
        require_id=require_id,
        skill_tool_type_value=skill_tool_type_value,
    ), runtime=_runtime,
    current_principal=current_principal,
)
message_type_aliases = _api_resources.message_type_aliases
normalize_message_type = _api_resources.normalize_message_type
serialize_resource = _api_resources.serialize_resource
serialize_file = _api_resources.serialize_file
validate_shared_file_payload = _api_resources.validate_shared_file_payload
normalize_chat_files = _api_resources.normalize_chat_files
require_no_shared_file_references = _api_resources.require_no_shared_file_references
resource_page = _api_resources.resource_page
enrich_resource_items = _api_resources.enrich_resource_items
resource_write_permission = _api_resources.resource_write_permission
lock_tenant_writes = _api_resources.lock_tenant_writes
save_resource = _api_resources.save_resource
delete_resource = _api_resources.delete_resource
register_resource_endpoints = _api_resources.register_resource_endpoints
all_resources = _api_resources.all_resources
empty_compatibility_page = _api_resources.empty_compatibility_page


from eic.api.health import bind_routes as bind_api_health, Dependencies as api_health_Dependencies
_api_health = bind_api_health(app, 
    dependencies=api_health_Dependencies(
    ), runtime=_runtime,
)
health = _api_health.health
readiness = _api_health.readiness


from eic.api.authorization import bind_routes as bind_authorization_routes
_authorization_routes = bind_authorization_routes(app,
    attach_operation_logs=attach_operation_logs,
    serialize_department=serialize_department,
    serialize_position=serialize_position,
)
get_token = _authorization_routes.get_token
logout = _authorization_routes.logout
register_options = _authorization_routes.register_options
register_user = _authorization_routes.register_user


from eic.api.models import bind_routes as bind_models_routes
_models_routes = bind_models_routes(app,
    probe_model_connection=probe_model_connection,
    current_principal=current_principal,
    ensure_permission=ensure_permission,
    first_int=first_int,
    legacy_model_type=legacy_model_type,
    legacy_provider=legacy_provider,
    normalize_endpoint=normalize_endpoint,
    normalize_model_type=normalize_model_type,
    normalize_provider=normalize_provider,
    page_result=page_result,
)
serialize_model = _models_routes.serialize_model
ai_models_page = _models_routes.ai_models_page
ai_models_list = _models_routes.ai_models_list
model_defaults_get = _models_routes.model_defaults_get
model_defaults_save = _models_routes.model_defaults_save
ai_models_add_edit = _models_routes.ai_models_add_edit
ai_models_delete = _models_routes.ai_models_delete
model_delete_impact = _models_routes.model_delete_impact
model_pending_references = _models_routes.model_pending_references
model_secret = _models_routes.model_secret
model_fetch_models = _models_routes.model_fetch_models
ai_models_test_connection = _models_routes.ai_models_test_connection


RESOURCE_ENDPOINTS = {
    "AIApps": "ai_apps", "AIPrompts": "ai_prompts",
    "AISkillToolManagement": "skills", "Dictionary": "dictionaries",
}


for legacy_prefix, type_name in RESOURCE_ENDPOINTS.items():
    register_resource_endpoints(legacy_prefix, type_name)


from eic.api.messages import bind_routes as bind_api_messages, Dependencies as api_messages_Dependencies
_api_messages = bind_api_messages(app, 
    dependencies=api_messages_Dependencies(
        delete_resource=delete_resource,
        ensure_permission=ensure_permission,
        lock_tenant_writes=lock_tenant_writes,
        resource_page=resource_page,
        save_resource=save_resource,
    ), runtime=_runtime,
    current_principal=current_principal,
)
message_private_system_page = _api_messages.message_private_system_page
message_private_user_page = _api_messages.message_private_user_page
message_ai_user_page = _api_messages.message_ai_user_page
message_page = _api_messages.message_page
message_add_edit = _api_messages.message_add_edit
dictionary_type_list = _api_messages.dictionary_type_list
dictionary_type_where_list = _api_messages.dictionary_type_where_list
http_log_page = _api_messages.http_log_page
os_log_page = _api_messages.os_log_page
message_system_page = _api_messages.message_system_page
message_announcement_page = _api_messages.message_announcement_page
message_unread_count = _api_messages.message_unread_count
message_read = _api_messages.message_read
message_delete_legacy = _api_messages.message_delete_legacy


from eic.api.skills import bind_routes as bind_api_skills, Dependencies as api_skills_Dependencies
_api_skills = bind_api_skills(app, 
    dependencies=api_skills_Dependencies(
        AgentBudget=lambda *args, **kwargs: AgentBudget(*args, **kwargs),
        HttpMcpClient=lambda *args, **kwargs: HttpMcpClient(*args, **kwargs),
        authorize_chat_mcp_resource=lambda *args, **kwargs: authorize_chat_mcp_resource(*args, **kwargs),
        decrypt_secret=decrypt_secret,
        discover_chat_mcp=lambda *args, **kwargs: discover_chat_mcp(*args, **kwargs),
        ensure_permission=lambda *args, **kwargs: ensure_permission(*args, **kwargs),
        file_source_path=file_source_path,
        has_permission=has_permission,
        load_chat_skill_packages=lambda *args, **kwargs: load_chat_skill_packages(*args, **kwargs),
        lock_tenant_writes=lock_tenant_writes,
        new_id=lambda *args, **kwargs: new_id(*args, **kwargs),
        permission_codes=permission_codes,
        reliable_tool=lambda *args, **kwargs: reliable_tool(*args, **kwargs),
        require_runtime_chat_permission=lambda *args, **kwargs: require_runtime_chat_permission(*args, **kwargs),
        runtime_app_config=lambda *args, **kwargs: runtime_app_config(*args, **kwargs),
        serialize_resource=serialize_resource,
    ), runtime=_runtime,
    current_principal=current_principal,
)
skills_list = _api_skills.skills_list
mcp_list = _api_skills.mcp_list
mcp_execution_capabilities = _api_skills.mcp_execution_capabilities
mcp_add_edit = _api_skills.mcp_add_edit
mcp_delete = _api_skills.mcp_delete
mcp_call = _api_skills.mcp_call
_validated_mcp_config_digest = _api_skills._validated_mcp_config_digest
mcp_network_scope = _api_skills.mcp_network_scope
_mcp_call_service = _api_skills._mcp_call_service
_mcp_call_guarded = _api_skills._mcp_call_guarded
refresh_local_skills = _api_skills.refresh_local_skills
load_declarative_skill = _api_skills.load_declarative_skill
authorized_skill_tools = _api_skills.authorized_skill_tools
skill_execution_catalog = _api_skills.skill_execution_catalog
install_builtin_skills = _api_skills.install_builtin_skills
execute_skill = _api_skills.execute_skill
set_builtin_skill_enabled = _api_skills.set_builtin_skill_enabled
execute_tool = _api_skills.execute_tool
get_tool_audits = _api_skills.get_tool_audits


from eic.api.memory import bind_routes as bind_memory_routes, SESSION_MEMORY_TYPES as SESSION_MEMORY_TYPES
_memory_routes = bind_memory_routes(app,
    current_principal=current_principal,
    ensure_permission=lambda *args, **kwargs: ensure_permission(*args, **kwargs),
    first_int=first_int,
    require_id=require_id,
    require_owned_chat_session=lambda *args, **kwargs: require_owned_chat_session(*args, **kwargs),
    runtime_app_config=lambda *args, **kwargs: runtime_app_config(*args, **kwargs),
)
_memory_datetime = _memory_routes._memory_datetime
_memory_score = _memory_routes._memory_score
_memory_priority = _memory_routes._memory_priority
_memory_payload = _memory_routes._memory_payload
_owned_memory = _memory_routes._owned_memory
memory_save = _memory_routes.memory_save
memory_search = _memory_routes.memory_search
memory_reindex = _memory_routes.memory_reindex
memory_archive = _memory_routes.memory_archive
memory_restore = _memory_routes.memory_restore
memory_delete = _memory_routes.memory_delete
memory_list = _memory_routes.memory_list
memory_confirm = _memory_routes.memory_confirm
memory_reject = _memory_routes.memory_reject


from eic.api.conversations import bind_routes as bind_api_conversations, Dependencies as api_conversations_Dependencies
_api_conversations = bind_api_conversations(app, 
    dependencies=api_conversations_Dependencies(
        attachment_error_message=lambda *args, **kwargs: attachment_error_message(*args, **kwargs),
        delete_resource=delete_resource,
        ensure_permission=ensure_permission,
        first_int=first_int,
        has_permission=has_permission,
        lock_tenant_writes=lock_tenant_writes,
        new_entity_id=new_entity_id,
        new_id=new_id,
        normalize_chat_files=normalize_chat_files,
        page_result=page_result,
        require_id=require_id,
        resource_page=resource_page,
        runtime_app_config=lambda *args, **kwargs: runtime_app_config(*args, **kwargs),
        save_resource=save_resource,
        serialize_file=serialize_file,
    ), runtime=_runtime,
    current_principal=current_principal,
)
chats_page = _api_conversations.chats_page
chats_add = _api_conversations.chats_add
chats_edit = _api_conversations.chats_edit
chat_citation_source = _api_conversations.chat_citation_source
conversation_label_model_get = _api_conversations.conversation_label_model_get
conversation_label_model_save = _api_conversations.conversation_label_model_save
conversation_labels_list = _api_conversations.conversation_labels_list
conversation_labels_generate = _api_conversations.conversation_labels_generate
conversation_label_rename = _api_conversations.conversation_label_rename
chats_delete = _api_conversations.chats_delete
require_owned_chat_session = _api_conversations.require_owned_chat_session
require_owned_chat_request = _api_conversations.require_owned_chat_request
normalize_chat_request_data = _api_conversations.normalize_chat_request_data
chats_clear_mine = _api_conversations.chats_clear_mine
chat_reasoning_context = _api_conversations.chat_reasoning_context
chat_reasoning_get = _api_conversations.chat_reasoning_get
chat_reasoning_save = _api_conversations.chat_reasoning_save
chat_history_page = _api_conversations.chat_history_page
chat_history_add = _api_conversations.chat_history_add
chat_history_delete = _api_conversations.chat_history_delete
chat_history_compact = _api_conversations.chat_history_compact
chat_context_status = _api_conversations.chat_context_status


from eic.ai.chat_events import bind_routes as bind_ai_chat_events, Dependencies as ai_chat_events_Dependencies
_ai_chat_events = bind_ai_chat_events(
    dependencies=ai_chat_events_Dependencies(
        attachment_error_message=lambda *args, **kwargs: attachment_error_message(*args, **kwargs),
        new_id=new_id,
        require_owned_chat_request=require_owned_chat_request,
    ), runtime=_runtime,
)
sse_payload = _ai_chat_events.sse_payload
compact_citations = _ai_chat_events.compact_citations
sse_message = _ai_chat_events.sse_message
persist_chat_event = _ai_chat_events.persist_chat_event
audited_sse_message = _ai_chat_events.audited_sse_message
mark_chat_cancelled = _ai_chat_events.mark_chat_cancelled
finalize_chat_response = _ai_chat_events.finalize_chat_response
estimate_tokens = _ai_chat_events.estimate_tokens
is_chat_cancelled = _ai_chat_events.is_chat_cancelled
next_chat_event_sequence = _ai_chat_events.next_chat_event_sequence


from eic.ai.chat_context import bind_routes as bind_ai_chat_context, Dependencies as ai_chat_context_Dependencies
_ai_chat_context = bind_ai_chat_context(
    dependencies=ai_chat_context_Dependencies(
        AgentBudget=lambda *args, **kwargs: AgentBudget(*args, **kwargs),
        app_response_schema=lambda *args, **kwargs: app_response_schema(*args, **kwargs),
        authorized_skill_tools=authorized_skill_tools,
        coerce_id=coerce_id,
        ensure_permission=ensure_permission,
        has_permission=has_permission,
        load_declarative_skill=load_declarative_skill,
        mcp_network_scope=mcp_network_scope,
        require_runtime_chat_permission=lambda *args, **kwargs: require_runtime_chat_permission(*args, **kwargs),
        retrieve_knowledge=retrieve_knowledge,
        runtime_app_config=lambda *args, **kwargs: runtime_app_config(*args, **kwargs),
    ), runtime=_runtime,
)
retrieve_chat_documents = _ai_chat_context.retrieve_chat_documents
retrieve_chat_documents_many = _ai_chat_context.retrieve_chat_documents_many
load_prompt_contents = _ai_chat_context.load_prompt_contents
require_runtime_chat_permission = _ai_chat_context.require_runtime_chat_permission
app_response_schema = _ai_chat_context.app_response_schema
runtime_app_config = _ai_chat_context.runtime_app_config
load_chat_tool_names = _ai_chat_context.load_chat_tool_names
load_chat_skill_packages = _ai_chat_context.load_chat_skill_packages
authorize_chat_mcp_resource = _ai_chat_context.authorize_chat_mcp_resource
discover_chat_mcp = _ai_chat_context.discover_chat_mcp
restore_mcp_bindings = _ai_chat_context.restore_mcp_bindings
skill_tool_name = _ai_chat_context.skill_tool_name
chat_skill_callable = _ai_chat_context.chat_skill_callable


from eic.api.chat_approvals import bind_routes as bind_api_chat_approvals, Dependencies as api_chat_approvals_Dependencies
_api_chat_approvals = bind_api_chat_approvals(app, 
    dependencies=api_chat_approvals_Dependencies(
        authorize_chat_mcp_resource=authorize_chat_mcp_resource,
        chat_skill_callable=chat_skill_callable,
        ensure_permission=ensure_permission,
        finish_approved_tool=lambda *args, **kwargs: finish_approved_tool(*args, **kwargs),
        first_int=first_int,
        has_permission=has_permission,
        load_chat_skill_packages=load_chat_skill_packages,
        load_chat_tool_names=load_chat_tool_names,
        load_prompt_contents=load_prompt_contents,
        new_id=new_id,
        normalize_chat_files=normalize_chat_files,
        reliable_tool=lambda *args, **kwargs: reliable_tool(*args, **kwargs),
        require_owned_chat_request=require_owned_chat_request,
        require_runtime_chat_permission=require_runtime_chat_permission,
        restore_mcp_bindings=restore_mcp_bindings,
        runtime_app_config=runtime_app_config,
        skill_tool_name=skill_tool_name,
    ), runtime=_runtime,
    current_principal=current_principal,
)
approval_model_digest = _api_chat_approvals.approval_model_digest
make_approval_snapshot = _api_chat_approvals.make_approval_snapshot
validate_approval_context = _api_chat_approvals.validate_approval_context
require_approval_run = _api_chat_approvals.require_approval_run
chat_get_approval = _api_chat_approvals.chat_get_approval
approval_write_session = _api_chat_approvals.approval_write_session
chat_decide_approval = _api_chat_approvals.chat_decide_approval
chat_continue_approved = _api_chat_approvals.chat_continue_approved
chat_get_trace = _api_chat_approvals.chat_get_trace


from eic.ai.chat_tools import bind_routes as bind_ai_chat_tools, Dependencies as ai_chat_tools_Dependencies
_ai_chat_tools = bind_ai_chat_tools(
    dependencies=ai_chat_tools_Dependencies(
        new_id=new_id,
        require_runtime_chat_permission=require_runtime_chat_permission,
    ), runtime=_runtime,
)
finish_approved_tool = _ai_chat_tools.finish_approved_tool
begin_chat_tool_call = _ai_chat_tools.begin_chat_tool_call
record_chat_tool_result = _ai_chat_tools.record_chat_tool_result


from eic.api.chat_stream import bind_routes as bind_api_chat_stream, Dependencies as api_chat_stream_Dependencies
_api_chat_stream = bind_api_chat_stream(app, 
    dependencies=api_chat_stream_Dependencies(
        ensure_permission=ensure_permission,
        has_permission=has_permission,
        require_id=require_id,
        require_owned_chat_request=require_owned_chat_request,
        require_owned_chat_session=require_owned_chat_session,
        sse_events=lambda *args, **kwargs: sse_events(*args, **kwargs),
    ), runtime=_runtime,
    current_principal=current_principal,
)
chat_history_cancel = _api_chat_stream.chat_history_cancel
chat_events = _api_chat_stream.chat_events
chat_budget = _api_chat_stream.chat_budget
attachment_error_message = _api_chat_stream.attachment_error_message
chat_capabilities = _api_chat_stream.chat_capabilities
chat_resume = _api_chat_stream.chat_resume
chat_stream = _api_chat_stream.chat_stream


from eic.ai.chat_execution import bind_routes as bind_ai_chat_execution, Dependencies as ai_chat_execution_Dependencies
_ai_chat_execution = bind_ai_chat_execution(
    dependencies=ai_chat_execution_Dependencies(
        AgentBudget=lambda *args, **kwargs: AgentBudget(*args, **kwargs),
        _mcp_call_service=_mcp_call_service,
        app_response_schema=app_response_schema,
        audited_sse_message=audited_sse_message,
        authorize_chat_mcp_resource=authorize_chat_mcp_resource,
        begin_chat_tool_call=begin_chat_tool_call,
        chat_skill_callable=chat_skill_callable,
        coerce_id_list=coerce_id_list,
        compact_citations=compact_citations,
        discover_chat_mcp=discover_chat_mcp,
        ensure_permission=ensure_permission,
        estimate_tokens=estimate_tokens,
        finalize_chat_response=finalize_chat_response,
        first_int=first_int,
        is_chat_cancelled=is_chat_cancelled,
        load_chat_skill_packages=load_chat_skill_packages,
        load_chat_tool_names=load_chat_tool_names,
        load_prompt_contents=load_prompt_contents,
        make_approval_snapshot=make_approval_snapshot,
        memory_save=lambda *args, **kwargs: memory_save(*args, **kwargs),
        next_chat_event_sequence=next_chat_event_sequence,
        normalize_chat_files=normalize_chat_files,
        record_chat_tool_result=record_chat_tool_result,
        reliable_tool=lambda *args, **kwargs: reliable_tool(*args, **kwargs),
        require_owned_chat_request=require_owned_chat_request,
        require_owned_chat_session=require_owned_chat_session,
        require_runtime_chat_permission=require_runtime_chat_permission,
        restore_mcp_bindings=restore_mcp_bindings,
        retrieve_chat_documents_many=lambda *args, **kwargs: retrieve_chat_documents_many(*args, **kwargs),
        runtime_app_config=runtime_app_config,
        skill_tool_name=skill_tool_name,
        stream_chat_completion=lambda *args, **kwargs: stream_chat_completion(*args, **kwargs),
        validate_approval_context=validate_approval_context,
        CHINESE_REPLY_INSTRUCTION=CHINESE_REPLY_INSTRUCTION,
    ), runtime=_runtime,
)
_sse_events_inner = _ai_chat_execution._sse_events_inner


from eic.ai.chat_producer import bind_routes as bind_ai_chat_producer, Dependencies as ai_chat_producer_Dependencies
_ai_chat_producer = bind_ai_chat_producer(
    dependencies=ai_chat_producer_Dependencies(
        launch_producer=lambda *args, **kwargs: _run_chat_producer(*args, **kwargs),
        _sse_events_inner=lambda *args, **kwargs: _sse_events_inner(*args, **kwargs),
        finalize_chat_response=finalize_chat_response,
        mark_chat_cancelled=lambda *args, **kwargs: mark_chat_cancelled(*args, **kwargs),
        new_id=new_id,
        require_owned_chat_request=lambda *args, **kwargs: require_owned_chat_request(*args, **kwargs),
        require_runtime_chat_permission=require_runtime_chat_permission,
        sse_message=sse_message,
    ), runtime=_runtime,
)
replay_chat_events = _ai_chat_producer.replay_chat_events
sse_events = _ai_chat_producer.sse_events
recover_agent_request = _ai_chat_producer.recover_agent_request
generation.configure_recovery(recover_agent_request)
_run_chat_producer = _ai_chat_producer._run_chat_producer
_leased_sse_events = _ai_chat_producer._leased_sse_events


from eic.api.tasks import bind_routes as bind_tasks_routes
_tasks_routes = bind_tasks_routes(app,
    current_principal=current_principal,
    ensure_permission=ensure_permission,
    lock_tenant_writes=lock_tenant_writes,
)
task_list = _tasks_routes.task_list
task_capabilities = _tasks_routes.task_capabilities
named_task = _tasks_routes.named_task
normalize_run_at = _tasks_routes.normalize_run_at
task_add_edit = _tasks_routes.task_add_edit
task_remove = _tasks_routes.task_remove
task_trigger = _tasks_routes.task_trigger
task_status = _tasks_routes.task_status
task_cancel = _tasks_routes.task_cancel


from eic.api.files import bind_routes as bind_api_files, Dependencies as api_files_Dependencies
_api_files = bind_api_files(app, 
    dependencies=api_files_Dependencies(
        FileResponse=FileResponse,
        attach_operation_logs=attach_operation_logs,
        ensure_permission=ensure_permission,
        file_source_path=file_source_path,
        lock_tenant_writes=lock_tenant_writes,
        page_result=page_result,
        require_no_shared_file_references=require_no_shared_file_references,
        save_uploads=save_uploads,
        serialize_file=serialize_file,
    ), runtime=_runtime,
    current_principal=current_principal,
)
file_upload = _api_files.file_upload
file_batch_upload = _api_files.file_batch_upload
file_upload_limits = _api_files.file_upload_limits
file_remote_upload = _api_files.file_remote_upload
file_by_id = _api_files.file_by_id
file_set_sharing = _api_files.file_set_sharing
file_delete_legacy = _api_files.file_delete_legacy
file_page = _api_files.file_page
file_download = _api_files.file_download
file_delete = _api_files.file_delete


from eic.api.frontend import bind_routes as bind_api_frontend, Dependencies as api_frontend_Dependencies
_api_frontend = bind_api_frontend(app, 
    dependencies=api_frontend_Dependencies(
        FileResponse=FileResponse,
    ), runtime=_runtime,
)
mount_frontend = _api_frontend.mount_frontend


bind_knowledge_extra_routes(app, current_principal=current_principal,
    new_entity_id=new_entity_id, queue_or_run_document=queue_or_run_document,
    validate_shared_file_payload=validate_shared_file_payload)
from . import appearance_preferences
appearance_preferences.bind_routes(app, current_principal, get_session)
from . import model_test_preferences
model_test_preferences.bind_routes(app, current_principal, get_session, ensure_permission)
code_generator.bind_routes(app, current_principal, get_session, ensure_permission)
observability_query.bind_routes(app, current_principal, get_session, ensure_permission)
experience.bind(app, current_principal, ensure_permission)
mount_frontend()

"""chat_context 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
import asyncio
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.core.runtime_bindings import RuntimeBindings
from eic.models import AIPrompt
from sqlalchemy.ext.asyncio import AsyncSession
from eic.ai.budget import ChatConfigurationError
from fastapi import HTTPException
from eic.models import KnowledgeBase
from eic.core.authentication import Principal
from eic.models import Resource
from eic.core.runtime_refs import RuntimeReferenceUnavailable
from eic.ai.standard_tools import StandardToolDefinition
from eic.models import User
from eic.ai import chat_mcp_tools
from eic.ai import context_access
from eic.ai.app_config import app_response_schema, runtime_app_config
from eic.core.langfuse_context import prompt_identity
from eic.core.runtime_refs import require_runtime_resource
from eic.ai.builtin_skills import resource_enabled
from sqlalchemy import select
from eic.core.authorization import require_runtime_chat_permission
from eic.ai.stream_reader import complete_cleanup
from eic.knowledge.multi_base import fuse_ranked_results


@dataclass(frozen=True)
class Dependencies:
    AgentBudget: Callable[..., Any]
    app_response_schema: Callable[..., Any]
    authorized_skill_tools: Callable[..., Any]
    coerce_id: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    has_permission: Callable[..., Any]
    load_declarative_skill: Callable[..., Any]
    mcp_network_scope: Callable[..., Any]
    require_runtime_chat_permission: Callable[..., Any]
    retrieve_knowledge: Callable[..., Any]
    runtime_app_config: Callable[..., Any]


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    async def retrieve_chat_documents(
        session: AsyncSession,
        principal: Principal,
        knowledge_base_id: object,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, object]]:
        """Build tenant-bound evidence for the LangGraph chat context."""
        base_id = dependencies.coerce_id(knowledge_base_id)
        if base_id is None:
            return []
        _, base = await require_runtime_resource(
            session, principal.tenant_id, base_id, "knowledge_bases", KnowledgeBase, require_dedicated=True,
        )
        async def before_external():
            async with runtime.sessions() as current:
                await dependencies.require_runtime_chat_permission(current, principal)
        try:
            payload = await dependencies.retrieve_knowledge(session, tenant_id=principal.tenant_id, base=base, query=query,
                                               limit=limit, before_external=before_external)
        except HTTPException as exc:
            if exc.status_code in {400, 409}:
                raise ChatConfigurationError("knowledge_configuration_error") from exc
            raise
        return payload.get("results") or []


    async def retrieve_chat_documents_many(
        session: AsyncSession,
        principal: Principal,
        knowledge_base_ids: list[int],
        query: str,
        *,
        limit: int = 8,
    ) -> list[dict[str, object]]:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        base_ids = list(dict.fromkeys(
            base_id for value in knowledge_base_ids
            if (base_id := dependencies.coerce_id(value)) is not None))
        if not base_ids:
            return []
        # 保留公开签名；调用者的会话可能持有其他 ORM 状态，不交给并发任务。
        batches = [None] * len(base_ids)
        pending_bases = iter(enumerate(base_ids))

        async def retrieve_worker():
            for index, base_id in pending_bases:
                async with runtime.sessions() as current:
                    await dependencies.require_runtime_chat_permission(current, principal)
                    rows = await retrieve_chat_documents(
                        current, principal, base_id, query, limit=limit)
                    batches[index] = (base_id, rows)

        tasks = [asyncio.create_task(retrieve_worker(), name="eic-chat-knowledge-retrieval")
                 for _ in range(min(2, len(base_ids)))]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # gather 失败不会自动取消其他任务；必须等待兄弟任务及其外呼清理完毕。
            for task in tasks:
                task.cancel()
            await complete_cleanup(asyncio.gather(*tasks, return_exceptions=True))
            raise
        async with runtime.sessions() as current:
            await dependencies.require_runtime_chat_permission(current, principal)
            for base_id in base_ids:
                await require_runtime_resource(current, principal.tenant_id, base_id,
                    "knowledge_bases", KnowledgeBase, require_dedicated=True)
        if len(batches) == 1:
            return batches[0][1][:limit]
        return fuse_ranked_results(batches, limit=limit)


    async def load_prompt_contents(session: AsyncSession, tenant_id: int, prompt_ids: list[int], *, identities=None) -> list[str]:
        contents: list[str] = []
        for prompt_id in prompt_ids:
            text = ""
            resource, dedicated = await require_runtime_resource(
                session, tenant_id, prompt_id, "ai_prompts", AIPrompt,
            )
            if resource is not None:
                text = str((resource.data or {}).get("content") or (resource.data or {}).get("prompt") or "")
            if not text:
                if dedicated is not None:
                    text = dedicated.content or ""
            if text.strip():
                contents.append(text)
                if identities is not None:
                    identities.append(prompt_identity(prompt_id=prompt_id))
        return contents


    async def load_chat_tool_names(
        session: AsyncSession, principal: Principal, app_id: int | None, config: dict[str, Any],
    ) -> set[str]:
        """只信任租户资源和 Agent 的开关/isSelect，不信任客户端名称描述。"""
        user = await session.get(User, principal.user_id)
        if user is None or user.tenant_id != principal.tenant_id or not user.is_active:
            return set()
        current = Principal(user.id, user.tenant_id, user.username, user.is_super_admin)
        if not await dependencies.has_permission("tool.execute", current, session):
            return set()
        skills = list(await session.scalars(select(Resource).where(
            Resource.tenant_id == principal.tenant_id,
            Resource.resource_type == "skills", Resource.is_deleted.is_(False),
        )))
        selected: set[int] = set()
        if app_id:
            for flag, field in (("isSkill", "skills"), ("isAITools", "tools")):
                if config.get(flag) is not True:
                    continue
                for item in config.get(field) or []:
                    if isinstance(item, dict) and item.get("isSelect") is True:
                        identifier = dependencies.coerce_id(item.get("aiSkillToolManagementId") or item.get("id"))
                        if identifier is not None:
                            selected.add(identifier)
        allowed = set()
        if dependencies.app_response_schema(config) is None:
            allowed.update(context_access.tool_names(config))
        for skill in skills:
            if not app_id or skill.id in selected:
                allowed.update(await dependencies.authorized_skill_tools(skill, current, session))
        return allowed


    async def load_chat_skill_packages(session, principal, app_id, config):
        if not app_id or not await dependencies.has_permission("skill.read", principal, session):
            return {}
        selected = set()
        if app_id and config.get("isSkill") is not True:
            return {}
        for item in config.get("skills") or []:
            if isinstance(item, dict) and item.get("isSelect") is True:
                value = dependencies.coerce_id(item.get("aiSkillToolManagementId") or item.get("id"))
                if value:
                    selected.add(value)
        resources = await session.scalars(select(Resource).where(
            Resource.tenant_id == principal.tenant_id, Resource.resource_type == "skills",
            Resource.is_deleted.is_(False)))
        packages = {}
        for resource in resources:
            if app_id and resource.id not in selected:
                continue
            if (resource.data or {}).get("executionMode") != "declarative-v1":
                continue
            if not resource_enabled(resource.data or {}):
                continue
            try:
                packages[resource.id] = await dependencies.load_declarative_skill(resource, principal, session)
            except (ValueError, OSError) as exc:
                raise RuntimeReferenceUnavailable("skill_package_changed") from exc
        return packages


    async def authorize_chat_mcp_resource(session, principal, app_id, config, resource_id):
        await dependencies.require_runtime_chat_permission(session, principal)
        await dependencies.ensure_permission("tool.execute", principal, session)
        await dependencies.ensure_permission("mcp.call", principal, session)
        current = await dependencies.runtime_app_config(session, principal.tenant_id, app_id)
        if current != config or resource_id not in chat_mcp_tools.selected_mcp_ids(current):
            raise RuntimeReferenceUnavailable("runtime_context_changed")
        resource = await session.get(Resource, resource_id, populate_existing=True)
        if (resource is None or resource.tenant_id != principal.tenant_id
                or resource.resource_type != "mcps" or resource.is_deleted
                or not resource_enabled(resource.data or {})):
            raise RuntimeReferenceUnavailable("mcp_unavailable")
        return resource


    async def discover_chat_mcp(principal, app_id, config, *, remaining_ms=30_000):
        if not app_id or not chat_mcp_tools.selected_mcp_ids(config):
            return [], []
        snapshots = []
        async def authorize(resource_id):
            async with runtime.sessions() as session:
                return await authorize_chat_mcp_resource(session, principal, app_id, config, resource_id)
        async def before_discovery(resource_id, remaining_ms):
            pass
        definitions = await chat_mcp_tools.discover_chat_mcp_tools(
            config, tenant_id=principal.tenant_id, authorize_resource=authorize,
            before_discovery=before_discovery, remaining_ms=min(30_000, remaining_ms), snapshots=snapshots,
            network_scope=lambda resource_id: dependencies.mcp_network_scope(principal, resource_id))
        return definitions, snapshots


    def restore_mcp_bindings(rows):
        return [chat_mcp_tools.ChatMcpSnapshot(
            row["resource_id"], row["config_digest"],
            tuple(StandardToolDefinition(**spec) for spec in row["definitions"])) for row in rows]


    def skill_tool_name(identifier, package):
        return f"skill_{identifier}_{package.content_hash[:12]}"


    def chat_skill_callable(package, allowed, external):
        if not package.data["steps"] or len(package.data["steps"]) >= dependencies.AgentBudget().max_tool_calls:
            return False
        specs = {spec.name: spec for spec in external}
        for name in package.data["allowed_tools"]:
            definition = specs.get(name) or runtime.tools.get(name)
            if name not in allowed or definition is None or definition.requires_approval:
                return False
        return True

    return _RouteExports(
        retrieve_chat_documents=retrieve_chat_documents,
        retrieve_chat_documents_many=retrieve_chat_documents_many,
        load_prompt_contents=load_prompt_contents,
        require_runtime_chat_permission=require_runtime_chat_permission,
        app_response_schema=app_response_schema,
        runtime_app_config=runtime_app_config,
        load_chat_tool_names=load_chat_tool_names,
        load_chat_skill_packages=load_chat_skill_packages,
        authorize_chat_mcp_resource=authorize_chat_mcp_resource,
        discover_chat_mcp=discover_chat_mcp,
        restore_mcp_bindings=restore_mcp_bindings,
        skill_tool_name=skill_tool_name,
        chat_skill_callable=chat_skill_callable,
    )

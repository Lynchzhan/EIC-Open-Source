"""每次副作用和流式批次前的完整引用校验；不跨 guard 缓存权限。"""
from dataclasses import dataclass
from typing import Any

from eic.core.runtime_bindings import RuntimeBindings
from eic.core.authentication import Principal
from eic.core.authorization import authorization_read_scope
from eic.core import generation
from eic.core.runtime_refs import RuntimeReferenceUnavailable, require_runtime_model, require_runtime_resource
from eic.models import AIModel, KnowledgeBase
from eic.ai import attachments as chat_attachments, chat_mcp_tools, conversation_context
from eic.ai.context_policy import context_settings
from eic.ai.memory_context import memory_snapshot_valid


@dataclass(frozen=True)
class RuntimeContextSnapshot:
    principal: Principal
    request_id: str
    app_id: int | None
    app_config: dict
    message_data: dict
    attachment_ids: list
    attachment_text: str
    attachment_snapshot: Any
    attachment_parse_cache: dict
    session_id: int
    before_id: int
    history_metadata: dict
    summary_context: Any
    prompt_ids: list
    prompt_contents: list
    mcp_snapshots: Any
    allowed_tool_names: set[str]
    skill_packages: dict
    memory_rows: list
    kms_ids: list[int]

    async def validate(self, current_model: AIModel, *, dependencies, runtime: RuntimeBindings,
                       check_cancelled=False) -> AIModel:
        # 新会话关闭后再外呼；fencing 写入由执行器的独立短事务负责。
        async with runtime.sessions() as current_session:
            with authorization_read_scope(current_session):
                if check_cancelled and await dependencies.is_chat_cancelled(
                        self.request_id, self.principal.tenant_id, session=current_session):
                    raise generation.LeaseLost("Generation cancelled")
                await dependencies.require_runtime_chat_permission(current_session, self.principal)
                await dependencies.require_owned_chat_request(current_session, self.principal, self.request_id)
                await dependencies.normalize_chat_files(self.message_data or {}, self.principal, current_session)
                if self.attachment_ids:
                    await dependencies.ensure_permission('file.read', self.principal, current_session)
                current_text, current_snapshot = await chat_attachments.load_context(
                    self.attachment_ids, self.principal, current_session, runtime.settings,
                    parsed_cache=self.attachment_parse_cache)
                if current_text != self.attachment_text or current_snapshot != self.attachment_snapshot:
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                if await dependencies.runtime_app_config(current_session, self.principal.tenant_id, self.app_id) != self.app_config:
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                if context_settings(self.app_config)["shortTermEnabled"] and not await conversation_context.history_snapshot_valid(
                        current_session, self.principal.tenant_id, self.principal.user_id, self.session_id, self.history_metadata):
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                if self.summary_context is not None:
                    _, current_summary, _ = await conversation_context.load_history(current_session,
                        tenant_id=self.principal.tenant_id, owner_id=self.principal.user_id,
                        session_id=self.session_id, before_id=self.before_id, config=self.app_config)
                    if current_summary != self.summary_context:
                        raise RuntimeReferenceUnavailable("runtime_context_changed")
                if await dependencies.load_prompt_contents(current_session, self.principal.tenant_id, self.prompt_ids) != self.prompt_contents:
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                current_names = await dependencies.load_chat_tool_names(
                    current_session, self.principal, self.app_id, self.app_config,
                )
                async def authorize_mcp(resource_id):
                    return await dependencies.authorize_chat_mcp_resource(
                        current_session, self.principal, self.app_id, self.app_config, resource_id)
                current_mcp = await chat_mcp_tools.revalidate_chat_mcp_snapshots(
                    self.mcp_snapshots, app_data=self.app_config, tenant_id=self.principal.tenant_id,
                    authorize_resource=authorize_mcp)
                current_names.update(spec.name for spec in current_mcp)
                packages = await dependencies.load_chat_skill_packages(current_session, self.principal, self.app_id, self.app_config)
                current_names.update(dependencies.skill_tool_name(identifier, package)
                    for identifier, package in packages.items()
                    if dependencies.chat_skill_callable(package, current_names, current_mcp))
                if current_names != self.allowed_tool_names:
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                if {key: value.content_hash for key, value in packages.items()} != {
                        key: value.content_hash for key, value in self.skill_packages.items()}:
                    raise RuntimeReferenceUnavailable("skill_package_changed")
                if not await memory_snapshot_valid(
                    current_session, self.memory_rows, tenant_id=self.principal.tenant_id,
                    owner_id=self.principal.user_id, session_id=self.session_id,
                    agent_id=dependencies.first_int(self.app_config.get("agentId")),
                ):
                    raise RuntimeReferenceUnavailable("runtime_context_changed")
                for base_id in self.kms_ids:
                    await require_runtime_resource(current_session, self.principal.tenant_id, base_id,
                                                   "knowledge_bases", KnowledgeBase, require_dedicated=True)
                return await require_runtime_model(current_session, self.principal.tenant_id, current_model.id, "chat")

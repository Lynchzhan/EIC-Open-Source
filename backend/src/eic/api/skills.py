"""skills 领域；通过显式装配提供跨域依赖。"""
from eic.api.resource_inputs import McpWrite, write_payload
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from eic.ai.builtin_skills import CATALOG as BUILTIN_SKILLS
from eic.api.schemas import BuiltinSkillInstall as BuiltinSkillInstall
from fastapi import Depends
from fastapi import HTTPException
from eic.ai.mcp_client import McpClientError
from pathlib import Path
from eic.core.authentication import Principal
from eic.models import Resource
from sqlalchemy.exc import SQLAlchemyError
from eic.api.schemas import SkillEnabledInput as SkillEnabledInput
from eic.api.schemas import SkillExecutionInput as SkillExecutionInput
from eic.models import ToolAudit
from eic.ai.tool_center import ToolExecutionError
from eic.core.tool_intent import ToolIntentConflict
from eic.core.tool_intent import ToolIntentInProgress
from eic.models import User
from eic.core import agent_runtime
from contextlib import asynccontextmanager
import asyncio
from eic.core.tool_intent import begin_tool_intent
from eic.ai.builtin_skills import builtin_data
from eic.ai import chat_mcp_tools
from eic.ai.stream_reader import complete_cleanup
from eic.ai import declarative_skills
from eic.core.security import encrypt_secret
from eic.core.tool_intent import finish_tool_intent
from eic.core.database import get_session
import hashlib
import json
import logging
import re
from eic.core.file_access import require_file_access
from eic.ai.builtin_skills import resource_enabled
from eic.ai.checkpoint_security import sanitize_tool_result
from sqlalchemy import select
import time
from eic.ai.builtin_skills import trusted_builtin
import uuid
from eic.core.file_access import valid_file_id
from eic.ai.reliable_calls import validate_tool_arguments


@dataclass(frozen=True)
class Dependencies:
    AgentBudget: Callable[..., Any]
    HttpMcpClient: Callable[..., Any]
    authorize_chat_mcp_resource: Callable[..., Any]
    decrypt_secret: Callable[..., Any]
    discover_chat_mcp: Callable[..., Any]
    ensure_permission: Callable[..., Any]
    file_source_path: Callable[..., Any]
    has_permission: Callable[..., Any]
    load_chat_skill_packages: Callable[..., Any]
    lock_tenant_writes: Callable[..., Any]
    new_id: Callable[..., Any]
    permission_codes: Callable[..., Any]
    reliable_tool: Callable[..., Any]
    require_runtime_chat_permission: Callable[..., Any]
    runtime_app_config: Callable[..., Any]
    serialize_resource: Callable[..., Any]


def bind_routes(app, *, dependencies: Dependencies, runtime: RuntimeBindings, current_principal):
    @app.get("/api/AISkillToolManagement/GetAllTools")
    @app.get("/api/AISkillToolManagement/GetAllSkills")
    async def skills_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("skill.read", principal, session)
        result = await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == "skills", Resource.is_deleted.is_(False)))
        return [dependencies.serialize_resource(item) for item in result]


    @app.get("/api/AISkillToolManagement/GetAllMcps")
    async def mcp_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("mcp.read", principal, session)
        result = await session.scalars(select(Resource).where(Resource.tenant_id == principal.tenant_id, Resource.resource_type == "mcps", Resource.is_deleted.is_(False)))
        return [dependencies.serialize_resource(item) for item in result]


    @app.get("/api/AISkillToolManagement/GetMcpExecutionCapabilities")
    async def mcp_execution_capabilities(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        codes = await dependencies.permission_codes(principal, session)
        if "mcp.read" not in codes and "*" not in codes:
            raise HTTPException(403, detail={"code": 403, "errMsg": "没有访问权限", "permission": "mcp.read"})
        user = await session.get(User, principal.user_id, populate_existing=True)
        return {"canMcpWrite": "mcp.write" in codes or "*" in codes,
                "canMcpCall": "mcp.call" in codes or "*" in codes,
                "isSuperAdmin": bool(user and user.is_active and user.tenant_id == principal.tenant_id
                                     and user.is_super_admin)}


    @app.post("/api/AISkillToolManagement/AddMcp")
    async def mcp_add_edit(payload: McpWrite, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        payload = write_payload(payload, McpWrite)
        await dependencies.ensure_permission("mcp.write", principal, session)
        user = await session.get(User, principal.user_id, populate_existing=True)
        is_super_admin = bool(user and user.is_active and user.tenant_id == principal.tenant_id
                              and (user.approval_status or "approved") == "approved" and user.is_super_admin)
        item = await session.get(Resource, int(payload["id"])) if payload.get("id") else None
        if item is not None and (item.tenant_id != principal.tenant_id or item.resource_type != "mcps"):
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "MCP 服务不存在"})
        endpoint = str(payload.get("endpoint") or payload.get("url") or "").strip()
        transport = str(payload.get("transport") or "http").lower()
        try:
            from eic.ai.tool_center import validate_mcp_config
            validate_mcp_config({"transport": transport, "endpoint": endpoint, "headers": {}})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": str(exc)}) from exc
        headers = payload.get("headers") or {}
        if not isinstance(headers, dict):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "headers 必须是对象"})
        clean = {"transport": transport, "endpoint": endpoint, "timeout": int(payload.get("timeout") or 30), "headerNames": sorted(str(key) for key in headers)}
        if not 1 <= clean["timeout"] <= 120:
            raise HTTPException(400, detail={"errMsg": "MCP timeout 必须介于 1 到 120 秒"})
        binding_changed = item is not None and (bool(headers) or any(
            clean.get(key) != (item.data or {}).get(key) for key in ("endpoint", "transport")))
        if binding_changed and not is_super_admin:
            # 旧白名单只授权旧服务，修改连接不能继承自动执行权限。
            clean["chatAutoTools"], clean["chatReadOnlyTools"] = [], []
        for field in ("chatAutoTools", "chatReadOnlyTools"):
            previous = (item.data or {}).get(field, []) if item is not None else []
            names = payload.get(field, [] if binding_changed and not is_super_admin else previous)
            if (not isinstance(names, list) or len(names) > 64
                    or any(not isinstance(name, str) or not name.strip() or len(name) > 128 for name in names)
                    or len(set(names)) != len(names)):
                raise HTTPException(400, detail={"errMsg": "聊天 MCP 工具授权名单无效"})
            if names and names != previous and not is_super_admin:
                raise HTTPException(403, detail={"errMsg": "聊天自动 MCP 工具须由系统管理员授权"})
            if binding_changed and not is_super_admin:
                names = []
            clean[field] = names
        clean["enabled"] = payload.get("enabled", (item.data or {}).get("enabled", True) if item else True)
        if type(clean["enabled"]) is not bool:
            raise HTTPException(400, detail={"errMsg": "enabled 必须为布尔值"})
        active_status = payload.get("activeStatus", (item.data or {}).get("activeStatus", 1) if item else 1)
        if type(active_status) is not int or active_status not in (0, 1):
            raise HTTPException(400, detail={"errMsg": "activeStatus 必须为 0 或 1"})
        clean["activeStatus"] = active_status
        if headers:
            clean["headers_encrypted"] = encrypt_secret(json.dumps({str(key): str(value) for key, value in headers.items()}, ensure_ascii=False))
        if item is None:
            item = Resource(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, resource_type="mcps", name=str(payload.get("name") or endpoint), owner_id=principal.user_id, data=clean)
            session.add(item)
        else:
            if not headers and (item.data or {}).get("headers_encrypted"):
                clean["headers_encrypted"] = item.data["headers_encrypted"]
            item.name, item.data = str(payload.get("name") or item.name), clean
        await session.commit()
        return True


    @app.delete("/api/AISkillToolManagement/DeleteMcp")
    async def mcp_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await dependencies.ensure_permission("mcp.write", principal, session)
        item = await session.get(Resource, Id)
        if item is None or item.tenant_id != principal.tenant_id or item.resource_type != "mcps":
            return False
        item.is_deleted = True
        await session.commit()
        return True


    @app.post("/api/AISkillToolManagement/CallMcp")
    async def mcp_call(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        try:
            return await _mcp_call_service(payload, principal, session)
        except ToolExecutionError as exc:
            # 准入/资源检查发生在传输异常映射之前，公开入口仍需返回稳定的拒绝响应。
            raise HTTPException(status_code=403, detail={
                "code": 403, "errMsg": "MCP 服务不可用", "errorCode": exc.code}) from None


    def _validated_mcp_config_digest(config):
        try:
            timeout = config.get("timeout", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ValueError("invalid_mcp_timeout")
            return chat_mcp_tools._config_digest(config)
        except (ValueError, TypeError, OverflowError):
            raise HTTPException(status_code=502, detail={
                "code": 502, "errMsg": "MCP 配置无效", "errorCode": "mcp_configuration_invalid"}) from None


    @asynccontextmanager
    async def mcp_network_scope(principal, resource_id):
        """在握手前记录准入和审计；工具结果仍由持久 intent 管理。"""
        from eic.core.dependency_controls import guard
        async with runtime.sessions() as audit_session:
            await dependencies.ensure_permission("mcp.call", principal, audit_session)
            resource = await audit_session.get(Resource, resource_id)
            if (resource is None or resource.tenant_id != principal.tenant_id
                    or resource.resource_type != "mcps" or resource.is_deleted
                    or not resource_enabled(resource.data or {})):
                raise ToolExecutionError("mcp_unavailable", "MCP 服务不可用")
            endpoint = (resource.data or {}).get("endpoint", "")
            admitted_digest = _validated_mcp_config_digest(resource.data or {})
        # 预留逻辑 SDK 操作成本；HTTP/通知/SSE 请求数不能由此推定。失败或取消不退还。
        async with guard("mcp", principal.tenant_id, endpoint=endpoint, reserve_units=66):
            async with runtime.sessions() as audit_session:
                audit_id = await dependencies.new_id(audit_session)
                audit_session.add(ToolAudit(
                    id=audit_id, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, tool_name="mcp_session", tool_type="mcp",
                    outcome="unknown", input_digest=agent_runtime.digest_payload({"resourceId": resource_id}),
                    duration_ms=0))
                await audit_session.commit()
            outcome = "unknown"
            started = time.monotonic()
            try:
                yield admitted_digest
                outcome = "success"
            finally:
                async def finish_session_audit():
                    async with runtime.sessions() as audit_session:
                        audit = await audit_session.get(ToolAudit, audit_id)
                        if audit is not None:
                            audit.outcome = outcome
                            audit.duration_ms = max(0, int((time.monotonic() - started) * 1000))
                            await audit_session.commit()
                await complete_cleanup(finish_session_audit())


    async def _mcp_call_service(payload, principal, session, *, expected_definition=None, authorize=None):
        async with mcp_network_scope(principal, int(payload.get("mcpId") or 0)) as admitted_digest:
            return await _mcp_call_guarded(payload, principal, session,
                expected_definition=expected_definition, authorize=authorize, admitted_digest=admitted_digest)


    async def _mcp_call_guarded(payload, principal, session, *, expected_definition=None, authorize=None, admitted_digest=None):
        from eic.core.tool_intent import ToolIntentConvergenceScope, converge_tool_intent_unknown
        from eic.ai.standard_tools import StandardToolDefinition
        await dependencies.ensure_permission("mcp.call", principal, session)
        item = await session.get(Resource, int(payload.get("mcpId") or 0))
        if item is None or item.tenant_id != principal.tenant_id or item.resource_type != "mcps" or item.is_deleted:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "MCP 服务不存在"})
        config = item.data or {}
        if _validated_mcp_config_digest(config) != admitted_digest:
            raise ToolExecutionError("mcp_configuration_changed", "MCP 准入后配置已变化，未连接")
        headers = {}
        if config.get("headers_encrypted"):
            try:
                headers = json.loads(dependencies.decrypt_secret(config["headers_encrypted"]))
            except (ValueError, TypeError):
                raise HTTPException(status_code=500, detail={"code": 500, "errMsg": "MCP 密钥无法解密"})
        decision, audit = None, None
        client = None
        convergence_scope = None
        result_committed = False
        convergence_error = "dispatch_outcome_unknown"
        async def authorize_mcp_dispatch():
            await dependencies.ensure_permission("mcp.call", principal, session)
            await session.refresh(item)
            if item.tenant_id != principal.tenant_id or item.is_deleted or item.data != config:
                raise ValueError("mcp_configuration_changed")
            if not resource_enabled(item.data or {}):
                raise ValueError("mcp_unavailable")
            if authorize is not None:
                await authorize(item)

        try:
            name = payload.get("name")
            arguments = payload.get("arguments", {})
            if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
                raise ValueError("invalid_mcp_arguments")
            timeout = config.get("timeout", 30)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 120:
                raise ValueError("invalid_mcp_timeout")
            await authorize_mcp_dispatch()
            client = dependencies.HttpMcpClient({"transport": config.get("transport"), "endpoint": config.get("endpoint"), "headers": headers}, timeout=timeout)
            await client.initialize()
            definition = next((tool for tool in await chat_mcp_tools._advertisements(client)
                               if tool.get("name") == name), None)
            if definition is None:
                raise McpClientError("MCP tool is not advertised", code="tool_not_allowed")
            schema = definition.get("inputSchema")
            if not isinstance(schema, dict):
                raise McpClientError("MCP tool schema is invalid", code="protocol_error")
            validate_tool_arguments(runtime.tools, arguments, schema)
            standard_name = f"mcp_{item.id}_{hashlib.sha256(name.encode()).hexdigest()[:16]}"
            timeout_ms = max(1, int(timeout * 1000))
            spec = StandardToolDefinition(name=standard_name, description=definition.get("description", ""),
                input_schema=schema, tool_type="mcp", source_id=str(item.id), source_name=name,
                timeout_ms=timeout_ms, idempotent=False)
            if expected_definition is not None:
                expected_definition.verify_mcp_advertisement(str(item.id), definition)
                if spec.name != expected_definition.name:
                    raise ToolExecutionError("tool_definition_changed", "MCP 工具绑定已变化")

            async def dispatch_mcp(call):
                nonlocal decision, audit, convergence_scope, result_committed
                call.definition.verify_mcp_advertisement(str(item.id), definition)
                normalized_input = {"mcpId": item.id, "name": name, "arguments": call.arguments,
                                    "endpoint": config.get("endpoint"), "transport": config.get("transport")}
                audit = ToolAudit(id=await dependencies.new_id(session), tenant_id=principal.tenant_id,
                    user_id=principal.user_id, tool_name=name, tool_type="mcp", outcome="intent",
                    input_digest=agent_runtime.digest_payload(normalized_input, redact=False), duration_ms=0)
                session.add(audit)
                def remember_mcp_claim(intent_id, input_digest):
                    nonlocal convergence_scope
                    convergence_scope = ToolIntentConvergenceScope(
                        intent_id, principal.tenant_id, principal.user_id, f"mcp:{name}",
                        input_digest, audit.id, name, audit.input_digest)
                decision = await begin_tool_intent(session, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, tool_name=f"mcp:{name}", normalized_input=normalized_input,
                    idempotency_key=payload.get("idempotencyKey"), authorize=authorize_mcp_dispatch,
                    on_prepared=remember_mcp_claim)
                if decision.replayed:
                    convergence_scope = None
                    return sanitize_tool_result(decision.result)
                await authorize_mcp_dispatch()
                async with asyncio.timeout(call.remaining_ms / 1000):
                    result = sanitize_tool_result(await client.call_tool(name, call.arguments))
                audit.outcome = "failed" if result.get("isError") else "success"
                audit.output_digest = agent_runtime.digest_payload(result, redact=False)
                await finish_tool_intent(session, intent_id=decision.intent_id, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, tool_name=f"mcp:{name}", result=result)
                result_committed = True
                return result

            standard_tools = runtime.tools.standard_tools({standard_name}, dispatch=dispatch_mcp, external=[spec])
            return await standard_tools.ainvoke(standard_name, arguments,
                call_id=f"mcp:{uuid.uuid4().hex}", remaining_ms=timeout_ms)
        except (ToolIntentConflict, ToolIntentInProgress) as exc:
            convergence_scope = None
            code = "duplicate_tool_call_id" if isinstance(exc, ToolIntentConflict) else "tool_outcome_uncertain"
            raise HTTPException(409, detail={"code": 409, "errMsg": "持久 MCP 意图禁止重复执行", "errorCode": code}) from exc
        except (McpClientError, ToolExecutionError, ValueError, TypeError, TimeoutError, OverflowError) as exc:
            result, outcome, error = {}, "failed", getattr(exc, "code", "mcp_request_invalid")
            if decision is not None:
                convergence_error = error
                raise HTTPException(status_code=502, detail={"code": 502, "errMsg": "MCP 调用结果待核对"}) from exc
        except SQLAlchemyError:
            convergence_error = "intent_persistence_failed"
            raise HTTPException(status_code=503, detail={"code": 503, "errMsg": "MCP 持久化不可用，结果待人工核对"}) from None
        finally:
            async def close_and_converge():
                try:
                    if client is not None:
                        await client.aclose()
                finally:
                    if convergence_scope is not None and not result_committed:
                        try:
                            # 先释放原事务；绝不在失败session上提交未知结果。
                            await session.rollback()
                        except (SQLAlchemyError, OSError, RuntimeError):
                            logging.getLogger(__name__).error("MCP transaction cleanup unavailable")
                        try:
                            await converge_tool_intent_unknown(runtime.sessions,
                                scope=convergence_scope, error_code=convergence_error)
                        except (SQLAlchemyError, ToolIntentConflict, ToolIntentInProgress,
                                OSError, RuntimeError, ValueError, TypeError):
                            logging.getLogger(__name__).error(
                                "MCP intent convergence unavailable; durable outcome requires manual review")
            await complete_cleanup(close_and_converge())
        raw_input = json.dumps({"mcpId": item.id, "name": payload.get("name"), "arguments": payload.get("arguments")}, ensure_ascii=False, sort_keys=True)
        audit = ToolAudit(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, user_id=principal.user_id, tool_name=str(payload.get("name") or ""), tool_type="mcp", outcome=outcome, input_digest=hashlib.sha256(raw_input.encode()).hexdigest(), output_digest=hashlib.sha256(json.dumps(result, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if result else None, duration_ms=0, error_message=error)
        session.add(audit)
        await session.commit()
        if error:
            raise HTTPException(status_code=502, detail={"code": 502, "errMsg": "MCP 调用失败"})
        return result


    @app.post("/api/AISkillToolManagement/RefreshLocalSkills")
    async def refresh_local_skills(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await dependencies.ensure_permission("skill.write", principal, session)
        """Refresh declarative local skills. Scripts are never imported during discovery."""
        if not principal.is_super_admin:
            raise HTTPException(status_code=403, detail={"code": 403, "errMsg": "需要系统管理员权限"})
        snapshot = runtime.skills.cache_snapshot(runtime.skill_root)
        loaded = 0
        for skill in snapshot["skills"]:
            if skill["name"] in BUILTIN_SKILLS:
                continue
            existing = await session.scalar(
                select(Resource).where(
                    Resource.tenant_id == principal.tenant_id,
                    Resource.resource_type == "skills",
                    Resource.name == skill["name"],
                    Resource.is_deleted.is_(False),
                )
            )
            manifest = Path(skill["path"])
            if existing is not None and ((existing.data or {}).get("fileId")
                    or (existing.data or {}).get("manifestPath") != str(manifest)):
                snapshot["failures"].append({"path": str(manifest), "error": "同名技能来源不同，未覆盖"})
                continue
            package = declarative_skills.parse_skill_document(manifest.read_text(encoding="utf-8"))
            data = {
                "description": skill["description"], "version": skill["version"], "enabled": skill["enabled"],
                "allowedTools": skill["allowed_tools"], "permissions": skill["permissions"],
                "manifestPath": skill["path"], **package.resource_data(),
            }
            if existing is None:
                session.add(Resource(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, resource_type="skills", name=skill["name"], owner_id=principal.user_id, data=data))
            else:
                data["enabled"] = resource_enabled(existing.data or {}) and package.data["enabled"]
                data["activeStatus"] = int(data["enabled"])
                existing.data = data
            loaded += 1
        await session.commit()
        return {"loaded": loaded, "failures": snapshot["failures"]}


    async def load_declarative_skill(skill, principal, session):
        data = skill.data or {}
        if (skill.tenant_id != principal.tenant_id or skill.resource_type != "skills"
                or skill.is_deleted or not resource_enabled(data)):
            raise ValueError("skill_unavailable")
        package = declarative_skills.restore_skill_package(data)
        if data.get("fileId"):
            await dependencies.ensure_permission("file.read", principal, session)
            file = await require_file_access(session, principal, valid_file_id(data["fileId"]), shared=True)
            def read_package():
                with dependencies.file_source_path(file).open("rb") as stream:
                    return declarative_skills.parse_skill_package(stream.read(131073))
            current = await asyncio.to_thread(read_package)
            if current.content_hash != package.content_hash:
                raise ValueError("skill_package_changed")
        elif data.get("manifestPath"):
            manifest = Path(data["manifestPath"])
            root = runtime.skill_root.resolve()
            if (manifest.is_symlink() or manifest.parent.is_symlink()
                    or not manifest.resolve().is_relative_to(root) or manifest.stat().st_size > 65536):
                raise ValueError("skill_local_binding_invalid")
            current = declarative_skills.parse_skill_document(manifest.read_text(encoding="utf-8"))
            if current.content_hash != package.content_hash:
                raise ValueError("skill_package_changed")
        else:
            raise ValueError("skill_package_binding_missing")
        return package


    async def authorized_skill_tools(skill: Resource, principal: Principal, session: AsyncSession) -> set[str]:
        data = skill.data or {}
        if skill.tenant_id != principal.tenant_id or skill.is_deleted or not resource_enabled(data):
            return set()
        if data.get("builtinKey") and trusted_builtin(skill) is None:
            return set()
        if data.get("executionMode") == "declarative-v1":
            try:
                package = await load_declarative_skill(skill, principal, session)
            except (ValueError, OSError, HTTPException):
                return set()
            tools = package.data["allowed_tools"]
            permissions = {"skill.read", "tool.execute"}
            for permission in permissions:
                if not await dependencies.has_permission(permission, principal, session):
                    return set()
            return {name for name in tools if runtime.tools.get(name) is not None}
        permissions = data.get("permissions", [])
        tools = data.get("allowedTools", [])
        if not isinstance(permissions, (list, tuple)) or not isinstance(tools, (list, tuple)):
            return set()
        for permission in permissions:
            if not isinstance(permission, str) or not await dependencies.has_permission(permission, principal, session):
                return set()
        return {name for name in tools if isinstance(name, str) and runtime.tools.get(name) is not None}


    @app.get("/api/AISkillToolManagement/GetExecutionCatalog")
    async def skill_execution_catalog(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("skill.read", principal, session)
        can_execute = await dependencies.has_permission("tool.execute", principal, session)
        resources = list(await session.scalars(select(Resource).where(
            Resource.tenant_id == principal.tenant_id, Resource.resource_type == "skills", Resource.is_deleted.is_(False))))
        skills, allowed = [], set()
        for resource in resources:
            names = await authorized_skill_tools(resource, principal, session)
            allowed.update(names)
            key = trusted_builtin(resource)
            declarative = (resource.data or {}).get("executionMode") == "declarative-v1"
            executable = bool(key and names)
            requires_app = False
            reason = ""
            if declarative and can_execute:
                try:
                    package = await load_declarative_skill(resource, principal, session)
                    async def catalog_dispatch(call):
                        raise ToolExecutionError("catalog_read_only", "执行目录不能调用工具")
                    declarative_skills.bind_skill(package, runtime.tools.standard_tools(names, dispatch=catalog_dispatch))
                    executable = True
                except (ValueError, OSError, HTTPException, ToolExecutionError):
                    executable = False
                    reason = "skill_binding_unavailable"
                else:
                    if any(runtime.tools.get(step["tool"]).requires_approval
                           for step in package.data["steps"] if runtime.tools.get(step["tool"]) is not None):
                        executable, reason = False, "skill_step_approval_not_supported"
                if not executable and reason == "skill_binding_unavailable":
                    try:
                        package = await load_declarative_skill(resource, principal, session)
                        missing = set(package.data["allowed_tools"]) - names
                        requires_app = bool(missing and all(re.fullmatch(r"mcp_[0-9]+_[0-9a-f]{16}", name)
                                                           for name in missing))
                        executable = (requires_app and await dependencies.has_permission("mcp.call", principal, session)
                                      and await dependencies.has_permission("ai.chat.write", principal, session))
                        if requires_app:
                            reason = "skill_mcp_app_required"
                    except (ValueError, OSError, HTTPException):
                        pass
            if declarative and executable:
                if any(runtime.tools.get(step["tool"]) is not None
                       and runtime.tools.get(step["tool"]).requires_approval
                       for step in package.data["steps"]):
                    executable, reason = False, "skill_step_approval_not_supported"
            skills.append({"id": resource.id, "name": resource.name, "builtinKey": key, "enabled": resource_enabled(resource.data or {}),
                            "executable": bool(can_execute and executable),
                            "requiresAppContext": requires_app, "executionReason": reason,
                            "executionMode": "deterministic" if key else "declarative-v1" if declarative else "metadata",
                           "example": BUILTIN_SKILLS[key]["example"] if key else None})
        return {"skills": skills, "tools": [{"name": tool.name, "description": tool.description,
            "inputSchema": tool.input_schema, "requiresApproval": tool.requires_approval,
            "executable": can_execute and not tool.requires_approval} for tool in runtime.tools.definitions(sorted(allowed))],
            "builtins": [{"key": key, "name": item["title"], "version": "1.0.0"} for key, item in BUILTIN_SKILLS.items()],
            "canInstall": principal.is_super_admin and await dependencies.has_permission("skill.write", principal, session),
            "canExecute": can_execute, "canWrite": await dependencies.has_permission("skill.write", principal, session),
            "canMcpWrite": await dependencies.has_permission("mcp.write", principal, session),
            "canMcpCall": await dependencies.has_permission("mcp.call", principal, session)}


    @app.post("/api/AISkillToolManagement/InstallBuiltinSkills")
    async def install_builtin_skills(payload: BuiltinSkillInstall, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("skill.write", principal, session)
        if not principal.is_super_admin:
            raise HTTPException(403, detail={"code": 403, "errMsg": "需要系统管理员权限"})
        if len(set(payload.keys)) != len(payload.keys) or any(key not in BUILTIN_SKILLS for key in payload.keys):
            raise HTTPException(422, detail={"code": 422, "errMsg": "技能选择无效"})
        await dependencies.lock_tenant_writes(session, principal.tenant_id)
        installed, existing = [], []
        for key in payload.keys:
            resource = await session.scalar(select(Resource).where(Resource.tenant_id == principal.tenant_id,
                Resource.resource_type == "skills", Resource.name == key, Resource.is_deleted.is_(False)))
            if resource is not None:
                if trusted_builtin(resource) != key:
                    raise HTTPException(409, detail={"code": 409, "errMsg": "同名自建技能不覆盖"})
                existing.append(resource.id)
                continue
            resource = Resource(id=await dependencies.new_id(session), tenant_id=principal.tenant_id, owner_id=principal.user_id,
                resource_type="skills", name=key, data=builtin_data(key))
            session.add(resource)
            installed.append(resource.id)
        await session.commit()
        return {"installed": installed, "existing": existing, "executionMode": "deterministic"}


    @app.post("/api/AISkillToolManagement/ExecuteSkill")
    async def execute_skill(payload: SkillExecutionInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("skill.read", principal, session)
        await dependencies.ensure_permission("tool.execute", principal, session)
        resource = await session.get(Resource, payload.skillId)
        if resource is None or resource.tenant_id != principal.tenant_id or resource.resource_type != "skills" or resource.is_deleted:
            raise HTTPException(404, detail={"code": 404, "errMsg": "技能不存在"})
        key = trusted_builtin(resource)
        allowed = await authorized_skill_tools(resource, principal, session)
        if (resource.data or {}).get("executionMode") == "declarative-v1":
            try:
                package = await load_declarative_skill(resource, principal, session)
            except (ValueError, OSError) as exc:
                raise HTTPException(409, detail={"errMsg": "技能声明或文件已变化"}) from exc
            expected_hash = package.content_hash
            execution_started = time.monotonic()
            config, definitions, snapshots = {}, [], []
            remote_names = set(package.data["allowed_tools"]) - allowed
            if remote_names:
                if payload.appId is None:
                    raise HTTPException(409, detail={"errorCode": "skill_mcp_app_required",
                        "errMsg": "MCP 技能步骤需要明确的应用授权上下文"})
                config = await dependencies.runtime_app_config(session, principal.tenant_id, payload.appId)
                selected = await dependencies.load_chat_skill_packages(session, principal, payload.appId, config)
                if resource.id not in selected or selected[resource.id].content_hash != expected_hash:
                    raise HTTPException(403, detail={"errMsg": "应用未选择当前技能"})
                # 关闭读取事务后才准入/发现，防止长期占用快照或锁。
                await session.rollback()
                definitions, snapshots = await dependencies.discover_chat_mcp(principal, payload.appId, config)
                allowed.update(spec.name for spec in definitions if spec.name in remote_names)
            skill_id = payload.skillId
            async def dispatch_step(call):
                async with runtime.sessions() as current_session:
                    await dependencies.ensure_permission("skill.read", principal, current_session)
                    await dependencies.ensure_permission("tool.execute", principal, current_session)
                    current = await current_session.get(Resource, skill_id)
                    if current is None:
                        raise ToolExecutionError("skill_unavailable", "技能不可用")
                    live = await load_declarative_skill(current, principal, current_session)
                    if live.content_hash != expected_hash:
                        raise ToolExecutionError("skill_package_changed", "技能版本已变化")
                    if call.definition.tool_type == "mcp":
                        await dependencies.require_runtime_chat_permission(current_session, principal)
                        await chat_mcp_tools.revalidate_chat_mcp_snapshots(
                            snapshots, app_data=config, tenant_id=principal.tenant_id,
                            authorize_resource=lambda resource_id: dependencies.authorize_chat_mcp_resource(
                                current_session, principal, payload.appId, config, resource_id))
                        selected = await dependencies.load_chat_skill_packages(current_session, principal, payload.appId, config)
                        if skill_id not in selected or selected[skill_id].content_hash != expected_hash:
                            raise ToolExecutionError("skill_package_changed", "应用技能绑定已变化")
                        async def authorize_step(item):
                            await dependencies.authorize_chat_mcp_resource(current_session, principal, payload.appId, config, item.id)
                            await current_session.refresh(current)
                            live_package = await load_declarative_skill(current, principal, current_session)
                            if live_package.content_hash != expected_hash:
                                raise ToolExecutionError("skill_package_changed", "技能已变化")
                            await chat_mcp_tools.revalidate_chat_mcp_snapshots(
                                snapshots, app_data=config, tenant_id=principal.tenant_id,
                                authorize_resource=lambda identifier: dependencies.authorize_chat_mcp_resource(
                                    current_session, principal, payload.appId, config, identifier))
                        return await _mcp_call_service({"mcpId": int(call.definition.source_id),
                            "name": call.definition.source_name, "arguments": call.arguments,
                            "idempotencyKey": call.call_id}, principal, current_session,
                            expected_definition=call.definition, authorize=authorize_step)
                    if call.definition.name not in await authorized_skill_tools(current, principal, current_session):
                        raise ToolExecutionError("tool_not_allowed", "步骤未获当前技能授权")
                    result = await execute_tool({"name": call.definition.name, "arguments": call.arguments,
                        "idempotencyKey": call.call_id, "timeoutMs": call.remaining_ms}, principal, current_session)
                    return result
            tools = runtime.tools.standard_tools(allowed, dispatch=dispatch_step, external=definitions)
            bound = declarative_skills.bind_skill(package, tools)
            return await bound.execute(payload.arguments,
                call_id=f"skill:{principal.tenant_id}:{principal.user_id}:{skill_id}:{payload.idempotencyKey}",
                remaining_ms=max(1, dependencies.AgentBudget().max_total_ms - int((time.monotonic() - execution_started) * 1000)))
        if key is None or BUILTIN_SKILLS[key]["tool"] not in allowed:
            raise HTTPException(409, detail={"code": 409, "errMsg": "技能未启用或未绑定受控执行器"})
        result = await execute_tool({"name": BUILTIN_SKILLS[key]["tool"], "arguments": payload.arguments,
            "idempotencyKey": f"skill-{resource.id}-{payload.idempotencyKey}"}, principal, session)
        return {**result, "skillId": resource.id, "skillKey": key, "version": "1.0.0", "executionMode": "deterministic"}


    @app.post("/api/AISkillToolManagement/SetBuiltinSkillEnabled")
    @app.post("/api/AISkillToolManagement/SetSkillEnabled")
    async def set_builtin_skill_enabled(payload: SkillEnabledInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await dependencies.ensure_permission("skill.write", principal, session)
        resource = await session.get(Resource, payload.skillId)
        if resource is None or resource.tenant_id != principal.tenant_id or resource.resource_type != "skills" or resource.is_deleted:
            raise HTTPException(404, detail={"code": 404, "errMsg": "技能不存在"})
        if trusted_builtin(resource) is None and (resource.data or {}).get("executionMode") != "declarative-v1":
            raise HTTPException(409, detail={"code": 409, "errMsg": "技能未绑定受控执行器"})
        resource.data = {**resource.data, "enabled": payload.enabled, "activeStatus": int(payload.enabled)}
        await session.commit()
        return True


    @app.post("/api/AISkillToolManagement/ExecuteTool")
    async def execute_tool(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        from eic.core.tool_intent import ToolIntentConvergenceScope, converge_tool_intent_unknown

        await dependencies.ensure_permission("tool.execute", principal, session)
        name = str(payload.get("name") or "")
        arguments = payload.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "arguments 必须是对象"})
        skills = await session.scalars(
            select(Resource).where(
                Resource.tenant_id == principal.tenant_id,
                Resource.resource_type == "skills",
                Resource.is_deleted.is_(False),
            )
        )
        allowed = set()
        for skill in skills:
            allowed.update(await authorized_skill_tools(skill, principal, session))
        async def authorize_dispatch():
            await dependencies.ensure_permission("tool.execute", principal, session)
            fresh = await session.scalars(select(Resource).where(
                Resource.tenant_id == principal.tenant_id,
                Resource.resource_type == "skills", Resource.is_deleted.is_(False),
            ).execution_options(populate_existing=True))
            current = set()
            for skill in fresh:
                current.update(await authorized_skill_tools(skill, principal, session))
            definition = runtime.tools.get(name)
            if name not in current or definition is None:
                raise ToolExecutionError("tool_not_allowed", "工具未获当前租户授权")
            validate_tool_arguments(runtime.tools, arguments, definition.input_schema)
            if definition.requires_approval:
                raise ToolExecutionError("approval_required", "该工具需要显式审批后执行")

        decision, audit = None, None
        convergence_scope = None
        result_committed = False
        convergence_error = "dispatch_outcome_unknown"
        try:
            timeout_ms = int(payload.get("timeoutMs") or 0) or None
            await authorize_dispatch()
            audit = ToolAudit(
                id=await dependencies.new_id(session), tenant_id=principal.tenant_id, user_id=principal.user_id,
                chat_id=payload.get("aIChatsId"), tool_name=name, tool_type="local", outcome="intent",
                input_digest=agent_runtime.digest_payload(arguments, redact=False), duration_ms=0,
            )
            session.add(audit)
            def remember_local_claim(intent_id, input_digest):
                nonlocal convergence_scope
                convergence_scope = ToolIntentConvergenceScope(
                    intent_id=intent_id, tenant_id=principal.tenant_id,
                    user_id=principal.user_id, tool_name=f"local:{name}",
                    input_digest=input_digest, audit_id=audit.id,
                    audit_tool_name=name, audit_input_digest=audit.input_digest,
                    audit_tool_type="local")
            decision = await begin_tool_intent(session, tenant_id=principal.tenant_id,
                user_id=principal.user_id, tool_name=f"local:{name}", normalized_input=arguments,
                idempotency_key=payload.get("idempotencyKey"), authorize=authorize_dispatch,
                on_prepared=remember_local_claim)
            if decision.replayed:
                convergence_scope = None
                return {**sanitize_tool_result(decision.result), "replayed": True}
            await authorize_dispatch()
            execution = await dependencies.reliable_tool(
                runtime.tools,
                name,
                arguments,
                allowed,
                tenant_id=principal.tenant_id, sessions=runtime.sessions,
                idempotency_key=(f"tenant:{principal.tenant_id}:user:{principal.user_id}:"
                                 f"{payload['idempotencyKey']}") if payload.get("idempotencyKey") else None,
                timeout_ms=min(timeout_ms, runtime.tools.get(name).timeout_ms)
                    if timeout_ms is not None and runtime.tools.get(name) is not None else timeout_ms,
            )
            audit.outcome, audit.output_digest, audit.duration_ms = "success", execution.output_digest, execution.elapsed_ms
            response = {"result": sanitize_tool_result(execution.value), "durationMs": execution.elapsed_ms, "replayed": execution.replayed,
                        "auditId": audit.id, "inputDigest": execution.input_digest, "outputDigest": execution.output_digest,
                        "executionOutputDigest": execution.output_digest,
                        "runtimeOutputDigest": agent_runtime.digest_payload(sanitize_tool_result(execution.value))}
            await finish_tool_intent(session, intent_id=decision.intent_id, tenant_id=principal.tenant_id,
                user_id=principal.user_id, tool_name=f"local:{name}", result=response)
            result_committed = True
            return response
        except asyncio.CancelledError:
            convergence_error = "dispatch_cancelled_outcome_unknown"
            raise
        except (ToolIntentConflict, ToolIntentInProgress) as exc:
            convergence_scope = None
            code = "duplicate_tool_call_id" if isinstance(exc, ToolIntentConflict) else "tool_outcome_uncertain"
            raise HTTPException(409, detail={"code": 409, "errMsg": "持久工具意图禁止重复执行", "errorCode": code}) from exc
        except ToolExecutionError as exc:
            convergence_error = exc.code
            try:
                session.add(ToolAudit(
                    id=await dependencies.new_id(session), tenant_id=principal.tenant_id, user_id=principal.user_id, chat_id=payload.get("aIChatsId"),
                    tool_name=name, tool_type="local", outcome="timeout" if exc.code == "tool_timeout" else "failed",
                    input_digest=exc.input_digest or hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(),
                    output_digest=None, duration_ms=exc.elapsed_ms, error_message=exc.code,
                ))
                if decision is not None and not decision.replayed:
                    if audit is not None:
                        audit.outcome, audit.error_message = "unknown" if exc.effect_status == "unknown" else "failed", exc.code
                    await finish_tool_intent(session, intent_id=decision.intent_id, tenant_id=principal.tenant_id,
                        user_id=principal.user_id, tool_name=f"local:{name}", error_code=exc.code)
                else:
                    await session.commit()
            except SQLAlchemyError:
                raise HTTPException(503, detail={"code": 503, "errMsg": "工具持久化不可用，结果待人工核对"}) from None
            status = 504 if exc.code == "tool_timeout" else 400 if exc.code == "invalid_tool_arguments" else 403 if exc.code == "tool_not_allowed" else 502
            raise HTTPException(status_code=status, detail={"code": status, "errMsg": str(exc), "errorCode": exc.code}) from exc
        except (PermissionError, ValueError, KeyError, TypeError):
            try:
                session.add(ToolAudit(
                    id=await dependencies.new_id(session), tenant_id=principal.tenant_id, user_id=principal.user_id, chat_id=payload.get("aIChatsId"),
                    tool_name=name, tool_type="local", outcome="denied", input_digest=hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest(),
                    output_digest=None, duration_ms=0, error_message="invalid_tool_request",
                ))
                await session.commit()
            except SQLAlchemyError:
                raise HTTPException(503, detail={"code": 503, "errMsg": "工具持久化不可用，结果待人工核对"}) from None
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "工具请求无效", "errorCode": "invalid_tool_request"}) from None
        except SQLAlchemyError:
            convergence_error = "intent_persistence_failed"
            raise HTTPException(status_code=503, detail={
                "code": 503, "errMsg": "工具持久化不可用，结果待人工核对"}) from None
        finally:
            if convergence_scope is not None and not result_committed:
                async def converge_local_unknown():
                    try:
                        await session.rollback()
                    except (SQLAlchemyError, OSError, RuntimeError):
                        logging.getLogger(__name__).error("Local tool transaction cleanup unavailable")
                    try:
                        await converge_tool_intent_unknown(
                            runtime.sessions, scope=convergence_scope, error_code=convergence_error)
                    except (SQLAlchemyError, ToolIntentConflict, ToolIntentInProgress,
                            OSError, RuntimeError, ValueError, TypeError):
                        logging.getLogger(__name__).error(
                            "Local tool intent convergence unavailable; durable outcome requires manual review")
                await complete_cleanup(converge_local_unknown())


    @app.get("/api/AISkillToolManagement/GetToolAudits")
    async def get_tool_audits(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await dependencies.ensure_permission("tool.execute", principal, session)
        audits = await session.scalars(
            select(ToolAudit).where(ToolAudit.tenant_id == principal.tenant_id).order_by(ToolAudit.created_at.desc()).limit(100)
        )
        return [
            {"id": audit.id, "toolName": audit.tool_name, "toolType": audit.tool_type, "outcome": audit.outcome, "durationMs": audit.duration_ms, "errorMessage": audit.error_message, "createdAt": audit.created_at.isoformat()}
            for audit in audits
        ]

    return _RouteExports(
        skills_list=skills_list,
        mcp_list=mcp_list,
        mcp_execution_capabilities=mcp_execution_capabilities,
        mcp_add_edit=mcp_add_edit,
        mcp_delete=mcp_delete,
        mcp_call=mcp_call,
        _validated_mcp_config_digest=_validated_mcp_config_digest,
        mcp_network_scope=mcp_network_scope,
        _mcp_call_service=_mcp_call_service,
        _mcp_call_guarded=_mcp_call_guarded,
        refresh_local_skills=refresh_local_skills,
        load_declarative_skill=load_declarative_skill,
        authorized_skill_tools=authorized_skill_tools,
        skill_execution_catalog=skill_execution_catalog,
        install_builtin_skills=install_builtin_skills,
        execute_skill=execute_skill,
        set_builtin_skill_enabled=set_builtin_skill_enabled,
        execute_tool=execute_tool,
        get_tool_audits=get_tool_audits,
    )

"""memory 领域路由；不导入应用入口或复制全局命名空间。"""
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
import hashlib
from typing import Any
from fastapi import Depends, HTTPException
from sqlalchemy import case, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from eic.core.authentication import Principal
from eic.core.database import get_session
from eic.core.ids import new_id
from eic.core import agent_runtime, memory_lifecycle, outbox
from eic.core.runtime_refs import require_runtime_resource
from eic.ai.context_policy import memory_settings
from eic.models import AIApp, AgentMemory, ChatMessage, OutboxEvent, Resource

SESSION_MEMORY_TYPES = {"short_term", "session"}
MEMORY_TYPES = SESSION_MEMORY_TYPES | {"long_term", "fact", "preference", "entity", "profile", "project", "experience"}


def bind_routes(app, *, current_principal, ensure_permission, first_int, require_id, require_owned_chat_session, runtime_app_config):
    def _memory_datetime(value: Any, label: str) -> datetime | None:
        if value is None or value == "":
            return None
        try:
            parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}格式无效"}) from exc
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


    def _memory_score(value: Any, label: str) -> float:
        if isinstance(value, bool):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}必须介于 0 和 1 之间"})
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}必须介于 0 和 1 之间"}) from exc
        if not 0 <= score <= 1:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": f"{label}必须介于 0 和 1 之间"})
        return score


    def _memory_priority(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "来源优先级必须介于 0 和 100 之间"})
        try:
            priority = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "来源优先级必须介于 0 和 100 之间"}) from exc
        if not 0 <= priority <= 100:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "来源优先级必须介于 0 和 100 之间"})
        return priority




    def _memory_payload(memory: AgentMemory) -> dict[str, Any]:
        return {
            "id": memory.id,
            "content": memory.content,
            "agentId": memory.agent_id,
            "sessionId": memory.session_id,
            "metadata": {k: v for k, v in (memory.attributes or {}).items() if not k.startswith("_")},
            "createdAt": memory.created_at.isoformat(),
            "memoryType": memory.memory_type,
            "memoryKey": memory.memory_key,
            "sourcePriority": memory.source_priority,
            "importance": memory.importance,
            "confidence": memory.confidence,
            "sourceMessageId": memory.source_message_id,
            "validFrom": memory.valid_from.isoformat() if memory.valid_from else None,
            "validUntil": memory.valid_until.isoformat() if memory.valid_until else None,
            "expiresAt": memory.expires_at.isoformat() if memory.expires_at else None,
            "status": memory.status,
            "version": memory.version,
            "contentHash": memory.content_hash,
            "indexStatus": memory.index_status,
            "supersedesId": memory.supersedes_id,
        }


    async def _owned_memory(memory_id: int, principal: Principal, session: AsyncSession) -> AgentMemory:
        await new_id(session)
        memory = await session.scalar(select(AgentMemory).where(
            AgentMemory.id == memory_id,
            AgentMemory.tenant_id == principal.tenant_id,
            AgentMemory.owner_id == principal.user_id,
        ))
        if memory is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "记忆不存在或不属于当前用户"})
        return memory


    @app.post("/api/AIAgentMemory/Save")
    async def memory_save(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, int | None]:
        await ensure_permission("memory.write", principal, session)
        content = str(payload.get("content", "")).strip()
        if not content:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记忆内容不能为空"})
        asynchronous = payload.get("asyncWrite", False)
        if type(asynchronous) is not bool or len(content.encode("utf-8")) > 65536:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "异步写入开关无效或记忆内容超过 64KB"})
        memory_type = str(payload.get("memoryType", "long_term") or "long_term").strip()
        if memory_type not in MEMORY_TYPES:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记忆类型无效"})
        session_id = None
        if payload.get("sessionId") not in (None, ""):
            session_id = require_id(payload.get("sessionId"), "会话 ID 无效")
            await require_owned_chat_session(session, principal, session_id)
        if memory_type in SESSION_MEMORY_TYPES and session_id is None:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "短期或会话记忆必须绑定会话"})
        agent_id = first_int(payload.get("agentId"))
        if agent_id:
            await require_runtime_resource(session, principal.tenant_id, agent_id, "ai_apps", AIApp)
        config = await runtime_app_config(session, principal.tenant_id, agent_id) if agent_id else {}
        if session_id:
            chat = await require_owned_chat_session(session, principal, session_id)
            config = await runtime_app_config(session, principal.tenant_id, chat.app_id)
            bound_agent = first_int(config.get("agentId"))
            if agent_id is not None and agent_id != bound_agent:
                raise HTTPException(400, detail={"errMsg": "记忆与会话智能体不一致"})
            agent_id = bound_agent
            if memory_settings(config)["mode"] == "off":
                raise HTTPException(409, detail={"errMsg": "当前智能体已关闭长期记忆"})
        if payload.get("metadata") is not None and not isinstance(payload["metadata"], dict):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记忆 metadata 必须为对象"})
        metadata = dict(payload.get("metadata") or {})
        if any(str(key).startswith("_") for key in metadata):
            raise HTTPException(400, detail={"errMsg": "metadata 不能使用内部保留字段"})
        memory_key = str(payload.get("memoryKey") or metadata.get("memoryKey") or "").strip() or None
        if memory_key is not None and len(memory_key) > 160:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记忆键长度不能超过 160 个字符"})
        settings = memory_settings(config)
        if agent_id and settings["mode"] == "off":
            raise HTTPException(409, detail={"errMsg": "当前智能体已关闭长期记忆"})
        from eic.ai.memory_candidates import validate_entry
        try:
            validate_entry(memory_type, memory_key, content, metadata, experience_enabled=settings["experienceEnabled"])
        except ValueError as exc:
            raise HTTPException(400, detail={"errMsg": str(exc)}) from None
        if len(content) > settings["maxMemoryChars"]:
            raise HTTPException(400, detail={"errMsg": "记忆内容超过智能体的单条长度限制"})
        if memory_type == "experience":
            raise HTTPException(400, detail={"errMsg": "经验请从有执行证据的候选中确认，不接受手工伪造证据"})
        source_priority = 100  # 用户明确保存；模型候选不能自报来源优先级。
        importance = _memory_score(payload.get("importance", 0.5), "重要性")
        confidence = _memory_score(payload.get("confidence", 0.5), "置信度")
        valid_from = _memory_datetime(payload.get("validFrom"), "生效时间")
        valid_until = _memory_datetime(payload.get("validUntil"), "失效时间")
        expires_at = _memory_datetime(payload.get("expiresAt"), "过期时间")
        if valid_from and valid_until and valid_from > valid_until:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "生效时间不能晚于失效时间"})
        source_message_id = payload.get("sourceMessageId")
        if source_message_id is not None:
            try:
                source_message_id = int(source_message_id)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "来源消息 ID 无效"}) from exc
            source = await session.get(ChatMessage, source_message_id)
            if source is None or source.tenant_id != principal.tenant_id:
                raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "来源消息不存在"})
            await require_owned_chat_session(session, principal, source.session_id)
            source_resource = await session.get(Resource, source.id)
            if (source.role != "user" or source.status != "completed" or (source_resource and source_resource.is_deleted)
                    or session_id is None or source.session_id != session_id):
                raise HTTPException(400, detail={"errMsg": "来源必须是当前会话中未删除的已完成用户消息"})
        write_key = payload.get("idempotencyKey") if asynchronous else None
        if write_key is not None and (not isinstance(write_key, str) or not write_key.strip() or len(write_key) > 160):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "幂等键必须为 1 至 160 个字符"})
        write_digest = agent_runtime.digest_payload({
            "content": content, "agentId": payload.get("agentId"), "sessionId": session_id,
            "memoryType": memory_type, "memoryKey": memory_key, "metadata": metadata,
            "sourcePriority": source_priority, "importance": importance, "confidence": confidence,
            "validFrom": valid_from, "validUntil": valid_until, "expiresAt": expires_at,
            "sourceMessageId": source_message_id,
        }, redact=False)
        memory_id = await new_id(session)
        previous = None
        if payload.get("supersedesId") not in (None, ""):
            previous = await _owned_memory(require_id(payload["supersedesId"], "旧记忆 ID 无效"), principal, session)
            if (previous.status != "active" or payload.get("expectedVersion") != previous.version
                    or previous.agent_id != agent_id or previous.memory_key != memory_key):
                raise HTTPException(409, detail={"errMsg": "记忆版本或作用域已变化，请刷新后纠正"})
            if asynchronous:
                raise HTTPException(400, detail={"errMsg": "纠正采用原子同步写入，不支持 asyncWrite"})
            valid_from = valid_from or datetime.now(UTC)
            if valid_from > datetime.now(UTC) or (previous.valid_from and valid_from < memory_lifecycle.utc(previous.valid_from)):
                raise HTTPException(400, detail={"errMsg": "纠正不能预设未来生效；请在生效时提交"})
            await memory_lifecycle.invalidate(session, previous, "corrected")
            previous.status = "superseded"
            previous.valid_until = valid_from
        event_key = (f"memory-write:{principal.user_id}:"
                     + hashlib.sha256(write_key.encode("utf-8")).hexdigest()) if write_key else f"memory-write:{memory_id}"
        if asynchronous and write_key:
            existing = await session.scalar(select(OutboxEvent).where(
                OutboxEvent.tenant_id == principal.tenant_id, OutboxEvent.event_key == event_key,
                OutboxEvent.kind == "memory-write",
            ))
            if existing:
                previous = await session.get(AgentMemory, existing.entity_id)
                if (previous is None or previous.owner_id != principal.user_id
                        or previous.tenant_id != principal.tenant_id
                        or (previous.attributes or {}).get("_writeDigest") != write_digest):
                    raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "幂等键已被不同或已撤销的记忆写入使用"})
                response = {"id": previous.id, "sessionId": previous.session_id, "writeJobId": existing.id}
                await session.rollback()
                return response
        await memory_lifecycle.capacity(session, principal.tenant_id, principal.user_id, agent_id, settings["maxActiveMemories"])
        memory = AgentMemory(
            id=memory_id, tenant_id=principal.tenant_id, owner_id=principal.user_id,
            agent_id=agent_id, session_id=session_id, content=content,
            version=(previous.version + 1) if previous else 1,
            supersedes_id=previous.id if previous else None,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            attributes={**metadata, "_writeDigest": write_digest} if asynchronous else metadata,
            memory_type=memory_type, importance=importance, confidence=confidence,
            memory_key=memory_key, source_priority=source_priority,
            source_message_id=source_message_id, valid_from=valid_from, valid_until=valid_until,
            expires_at=expires_at or datetime.now(UTC) + timedelta(days=min(30, settings["retentionDays"]) if memory_type == "project" else settings["retentionDays"]),
            status="pending" if asynchronous else "active",
        )
        session.add(memory)
        if not asynchronous:
            await memory_lifecycle.stage_index(session, memory,
                memory_settings(config)["embeddingModelId"])
        write_job_id = None
        if asynchronous:
            write_job_id = await outbox.stage(
                session, "memory-write", memory.id, principal.tenant_id,
                event_key=event_key,
            )
        memory_lifecycle.record(session, memory, "memory_save", memory.status)
        await session.commit()
        result = {"id": memory.id, "sessionId": memory.session_id}
        if asynchronous:
            result["writeJobId"] = write_job_id
        return result


    @app.get("/api/AIAgentMemory/Search")
    async def memory_search(
        query: str = "",
        principal: Principal = Depends(current_principal),
        session: AsyncSession = Depends(get_session),
        agentId: int | None = None,
        sessionId: int | None = None,
        memoryTypes: str | None = None,
        asOf: str | None = None,
    ) -> list[dict[str, Any]]:
        await ensure_permission("ai.chat.read", principal, session)
        if sessionId is not None:
            sessionId = require_id(sessionId, "会话 ID 无效")
            await require_owned_chat_session(session, principal, sessionId)
        requested_types = {item.strip() for item in (memoryTypes or "").split(",") if item.strip()}
        if requested_types and not requested_types <= MEMORY_TYPES:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "记忆类型无效"})
        now = _memory_datetime(asOf, "历史时点") if asOf else datetime.now(UTC)
        criteria = [
            AgentMemory.tenant_id == principal.tenant_id,
            AgentMemory.owner_id == principal.user_id,
            AgentMemory.status.in_(["active", "superseded"] if asOf else ["active"]),
            AgentMemory.created_at <= now,
            or_(AgentMemory.valid_from.is_(None), AgentMemory.valid_from <= now),
            or_(AgentMemory.valid_until.is_(None), AgentMemory.valid_until > now),
            or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > now),
            AgentMemory.memory_type.notin_(SESSION_MEMORY_TYPES) if sessionId is None else or_(
                AgentMemory.memory_type.notin_(SESSION_MEMORY_TYPES),
                AgentMemory.session_id == sessionId,
            ),
        ]
        if agentId is not None:
            criteria.append(AgentMemory.agent_id == agentId)
        if requested_types:
            criteria.append(AgentMemory.memory_type.in_(requested_types))
        ordering = (
            AgentMemory.source_priority.desc(), AgentMemory.valid_from.desc(),
            AgentMemory.confidence.desc(), AgentMemory.importance.desc(),
            AgentMemory.created_at.desc(), AgentMemory.id.desc(),
        )
        # 先在全部合格记忆内选出每个事实的赢家，再限制返回数量。
        # 无键记忆以自己的 ID 分区，避免与用户提供的事实键碰撞。
        memory_key = func.nullif(AgentMemory.memory_key, "")
        ranked = select(
            AgentMemory.id.label("memory_id"),
            func.row_number().over(
                partition_by=(AgentMemory.agent_id, memory_key, case((memory_key.is_(None), AgentMemory.id), else_=0)),
                order_by=ordering,
            ).label("position"),
        ).where(*criteria).subquery()
        selected = list(await session.scalars(
            select(AgentMemory).join(ranked, AgentMemory.id == ranked.c.memory_id)
            .where(ranked.c.position == 1, AgentMemory.content.contains(query, autoescape=True) if query else text("1=1"))
            .order_by(*ordering).limit(20)
        ))
        selected_keys = [item.memory_key for item in selected if item.memory_key]
        conflict_details: dict[str, dict[str, Any]] = {}
        if selected_keys:
            candidates = select(
                AgentMemory.id.label("memory_id"), AgentMemory.memory_key, AgentMemory.agent_id,
                func.count().over(partition_by=(AgentMemory.agent_id, AgentMemory.memory_key)).label("candidate_count"),
                func.row_number().over(
                    partition_by=(AgentMemory.agent_id, AgentMemory.memory_key), order_by=ordering,
                ).label("position"),
            ).where(*criteria, AgentMemory.memory_key.in_(selected_keys)).subquery()
            # 候选总数不截断；只限制审计响应中的 ID 列表，避免返回无限大的 JSON。
            rows = await session.execute(
                select(candidates).where(candidates.c.position <= 200)
                .order_by(candidates.c.memory_key, candidates.c.position)
            )
            for row in rows:
                detail = conflict_details.setdefault((row.agent_id, row.memory_key), {
                    "memoryKey": row.memory_key,
                    "candidateCount": row.candidate_count,
                    "candidateIds": [],
                    "candidateIdsTruncated": row.candidate_count > 200,
                    "resolution": "source_priority_then_valid_time_then_confidence_then_importance",
                })
                detail["candidateIds"].append(row.memory_id)
        result = []
        for memory in selected:
            item = _memory_payload(memory)
            conflict = conflict_details.get((memory.agent_id, memory.memory_key))
            if conflict and conflict["candidateCount"] > 1:
                item["conflict"] = {
                    **conflict,
                    "lowConfidence": memory.confidence < 0.5,
                    "warning": "存在低置信度冲突，建议人工确认" if memory.confidence < 0.5 else None,
                }
            result.append(item)
        return result


    @app.post("/api/AIAgentMemory/Reindex")
    async def memory_reindex(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("memory.write", principal, session)
        await new_id(session)
        agent_id = first_int(payload.get("agentId"))
        if payload.get("sessionId"):
            chat = await require_owned_chat_session(session, principal, require_id(payload["sessionId"], "会话 ID 无效"))
            if agent_id and agent_id != chat.app_id:
                raise HTTPException(400, detail={"errMsg": "会话与智能体不一致"})
            agent_id = chat.app_id
        config = await runtime_app_config(session, principal.tenant_id, agent_id) if agent_id else {}
        model_id = first_int(payload.get("embeddingModelId")) or first_int(memory_settings(config)["embeddingModelId"])
        if not model_id:
            raise HTTPException(400, detail={"errMsg": "请先配置记忆向量模型"})
        after_id = first_int(payload.get("afterId")) or 0
        rows = list(await session.scalars(select(AgentMemory).where(
            AgentMemory.tenant_id == principal.tenant_id, AgentMemory.owner_id == principal.user_id,
            AgentMemory.agent_id == agent_id, AgentMemory.status == "active", AgentMemory.id > after_id)
            .order_by(AgentMemory.id).limit(101)))
        staged = 0
        for memory in rows[:100]:
            staged += bool(await memory_lifecycle.stage_index(session, memory, model_id,
                budget_session_id=require_id(payload["sessionId"], "会话 ID 无效") if payload.get("sessionId") else None))
        await session.commit()
        return {"queued": staged, "hasMore": len(rows) > 100,
                "nextAfterId": str(rows[min(100, len(rows)) - 1].id) if rows else str(after_id)}


    @app.post("/api/AIAgentMemory/RebuildSearchIndex")
    async def memory_rebuild_search(payload: dict[str, Any], principal: Principal = Depends(current_principal),
                                    session: AsyncSession = Depends(get_session)):
        """显式有界回填；只读已有正文/向量，不产生模型请求。"""
        await ensure_permission("memory.write", principal, session)
        await new_id(session)
        agent_id = first_int(payload.get("agentId"))
        if payload.get("agentId") not in (None, ""):
            agent_id = require_id(payload["agentId"], "智能体 ID 无效")
            await require_runtime_resource(session, principal.tenant_id, agent_id, "ai_apps", AIApp)
        session_id = None
        if payload.get("sessionId") not in (None, ""):
            chat = await require_owned_chat_session(session, principal,
                require_id(payload["sessionId"], "会话 ID 无效"))
            if agent_id is not None and agent_id != chat.app_id:
                raise HTTPException(400, detail={"errMsg": "会话与智能体不一致"})
            session_id, agent_id = chat.id, chat.app_id
        after_id = require_id(payload["afterId"], "分页游标无效") if payload.get("afterId") else 0
        if not 0 <= after_id <= 2**63 - 1:
            raise HTTPException(400, detail={"errMsg": "分页游标无效"})
        rows = list(await session.scalars(select(AgentMemory).where(
            AgentMemory.tenant_id == principal.tenant_id, AgentMemory.owner_id == principal.user_id,
            AgentMemory.agent_id == agent_id, AgentMemory.id > after_id,
            or_(AgentMemory.memory_type.notin_(SESSION_MEMORY_TYPES), AgentMemory.session_id == session_id))
            .order_by(AgentMemory.id).limit(101)))
        from eic.core.memory_search import index_terms, stage_projection
        queued = 0
        for memory in rows[:100]:
            await index_terms(session, memory)
            queued += bool(await stage_projection(session, memory))
        await session.commit()
        return {"indexed": min(100, len(rows)), "projectionQueued": queued, "hasMore": len(rows) > 100,
                "nextAfterId": str(rows[min(100, len(rows)) - 1].id) if rows else str(after_id)}


    @app.post("/api/AIAgentMemory/Archive")
    async def memory_archive(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("memory.write", principal, session)
        memory = await _owned_memory(Id, principal, session)
        if memory.status != "active":
            raise HTTPException(409, detail={"errMsg": "只有生效记忆可以归档"})
        await memory_lifecycle.invalidate(session, memory, "archived")
        memory.status = "archived"
        await session.commit()
        return True


    @app.post("/api/AIAgentMemory/Restore")
    async def memory_restore(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("memory.write", principal, session)
        memory = await _owned_memory(Id, principal, session)
        if memory.status not in {"archived"}:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "仅普通归档记忆可以恢复；候选、拒绝及已遗忘记忆不能恢复"})
        now = datetime.now(UTC)
        if memory.expires_at is not None and memory_lifecycle.utc(memory.expires_at) <= now:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "记忆已过期，不能恢复"})
        if memory.valid_until is not None and memory_lifecycle.utc(memory.valid_until) <= now:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "记忆已失效，不能恢复"})
        config = await runtime_app_config(session, principal.tenant_id, memory.agent_id) if memory.agent_id else {}
        if memory.agent_id and memory_settings(config)["mode"] == "off":
            raise HTTPException(409, detail={"errMsg": "当前智能体已关闭长期记忆"})
        await memory_lifecycle.capacity(session, principal.tenant_id, principal.user_id,
                                        memory.agent_id, memory_settings(config)["maxActiveMemories"])
        memory.status = "active"
        await memory_lifecycle.stage_index(session, memory)
        await session.commit()
        return True


    @app.delete("/api/AIAgentMemory/Delete")
    async def memory_delete(Id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("memory.write", principal, session)
        memory = await _owned_memory(Id, principal, session)
        if memory.status == "forgotten":
            return True
        await memory_lifecycle.invalidate(session, memory, "user_forgotten", forget=True)
        await session.commit()
        return True


    @app.get("/api/AIAgentMemory/List")
    async def memory_list(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session),
                          agentId: int | None = None, sessionId: int | None = None, status: str = "active", afterId: int = 0,
                          memoryKey: str | None = None):
        await ensure_permission("ai.chat.read", principal, session)
        if status not in {"active", "candidate", "rejected", "archived", "forgotten", "superseded", "pending", "cancelled"}:
            raise HTTPException(400, detail={"errMsg": "记忆状态无效"})
        if sessionId:
            chat = await require_owned_chat_session(session, principal, sessionId)
            if agentId is not None and chat.app_id != agentId:
                raise HTTPException(400, detail={"errMsg": "会话与智能体不一致"})
            agentId = chat.app_id
        rows = list(await session.scalars(select(AgentMemory).where(
            AgentMemory.tenant_id == principal.tenant_id, AgentMemory.owner_id == principal.user_id,
            AgentMemory.agent_id == agentId, AgentMemory.status == status, AgentMemory.id > afterId,
            AgentMemory.memory_key == memoryKey if memoryKey is not None else text("1=1"),
            or_(AgentMemory.memory_type.notin_(SESSION_MEMORY_TYPES), AgentMemory.session_id == sessionId))
            .order_by(AgentMemory.id).limit(51)))
        return {"items": [_memory_payload(row) for row in rows[:50]], "hasMore": len(rows) > 50,
                "nextAfterId": str(rows[min(50, len(rows)) - 1].id) if rows else str(afterId)}


    @app.post("/api/AIAgentMemory/Confirm")
    async def memory_confirm(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("memory.write", principal, session)
        memory = await _owned_memory(require_id(payload.get("id"), "候选 ID 无效"), principal, session)
        if memory.status != "candidate" or payload.get("expectedVersion") != memory.version:
            raise HTTPException(409, detail={"errMsg": "候选状态或版本已变化，请刷新"})
        from eic.ai.memory_candidates import validate_candidate, validate_entry
        try:
            settings = await validate_candidate(session, memory)
            replacement = str(payload.get("content", memory.content)).strip()
            if not replacement or len(replacement) > settings["maxMemoryChars"]:
                raise ValueError("记忆内容为空或超过长度限制")
            validate_entry(memory.memory_type, memory.memory_key, replacement, memory.attributes,
                           experience_enabled=settings["experienceEnabled"])
        except ValueError as exc:
            raise HTTPException(409, detail={"errMsg": str(exc)}) from None
        now = datetime.now(UTC)
        if any(value is not None and memory_lifecycle.utc(value) <= now for value in (memory.expires_at, memory.valid_until)):
            raise HTTPException(409, detail={"errMsg": "候选已过期，请拒绝后重新提取"})
        conflicts = list(await session.scalars(select(AgentMemory).where(
            AgentMemory.tenant_id == principal.tenant_id, AgentMemory.owner_id == principal.user_id,
            AgentMemory.agent_id == memory.agent_id, AgentMemory.memory_key == memory.memory_key,
            AgentMemory.status == "active", or_(AgentMemory.valid_until.is_(None), AgentMemory.valid_until > now),
            or_(AgentMemory.expires_at.is_(None), AgentMemory.expires_at > now)))) if memory.memory_key else []
        if conflicts:
            if len(conflicts) != 1 or str(payload.get("supersedesId")) != str(conflicts[0].id) or payload.get("previousVersion") != conflicts[0].version:
                raise HTTPException(409, detail={"errMsg": "同一事实已有生效记忆；请选定旧项并确认其版本后再替换",
                                                 "conflictIds": [str(row.id) for row in conflicts]})
            previous = conflicts[0]
            await memory_lifecycle.invalidate(session, previous, "corrected")
            previous.status, previous.valid_until = "superseded", now
            memory.supersedes_id = previous.id
            memory.version = previous.version + 1
        elif payload.get("supersedesId"):
            raise HTTPException(409, detail={"errMsg": "待替换记忆已变化，请刷新"})
        await memory_lifecycle.capacity(session, principal.tenant_id, principal.user_id, memory.agent_id, settings["maxActiveMemories"])
        corrected = replacement != memory.content
        memory.content, memory.content_hash = replacement, hashlib.sha256(replacement.encode("utf-8")).hexdigest()
        memory.status, memory.source_priority, memory.confidence, memory.valid_from = "active", 100, 1.0, now
        memory.importance = _memory_score(payload.get("importance", memory.importance), "重要性")
        memory.attributes = {**memory.attributes, "userConfirmed": True, "userCorrected": corrected}
        await memory_lifecycle.stage_index(session, memory, settings["embeddingModelId"])
        memory_lifecycle.record(session, memory, "memory_confirmation", "completed")
        await session.commit()
        return _memory_payload(memory)


    @app.post("/api/AIAgentMemory/Reject")
    async def memory_reject(payload: dict[str, Any], principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("memory.write", principal, session)
        memory = await _owned_memory(require_id(payload.get("id"), "候选 ID 无效"), principal, session)
        if memory.status != "candidate" or payload.get("expectedVersion") != memory.version:
            raise HTTPException(409, detail={"errMsg": "候选状态或版本已变化，请刷新"})
        memory.status = "rejected"
        memory_lifecycle.record(session, memory, "memory_confirmation", "rejected")
        await session.commit()
        return True
    return SimpleNamespace(_memory_datetime=_memory_datetime, _memory_score=_memory_score, _memory_priority=_memory_priority, _memory_payload=_memory_payload, _owned_memory=_owned_memory, memory_save=memory_save, memory_search=memory_search, memory_reindex=memory_reindex, memory_archive=memory_archive, memory_restore=memory_restore, memory_delete=memory_delete, memory_list=memory_list, memory_confirm=memory_confirm, memory_reject=memory_reject)

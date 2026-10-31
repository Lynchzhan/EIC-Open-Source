"""tasks 领域路由；不导入应用入口或复制全局命名空间。"""
from types import SimpleNamespace
from datetime import UTC, datetime
import re
from typing import Any
from fastapi import Body, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from eic.core.authentication import Principal
from eic.core.database import get_session
from eic.core.ids import new_id
from eic.core import outbox, task_schedule
from eic.core import tasks as task_contract
from eic.core import queue as task_queue
from eic.models import OutboxEvent, Resource, ScheduledTask, TaskRun
from eic.api.schemas import TaskInput


def bind_routes(app, *, current_principal, ensure_permission, lock_tenant_writes):
    @app.post("/api/AITasks/GetTaskList")
    async def task_list(payload: dict[str, Any] | None = Body(default=None), principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
        await ensure_permission("task.read", principal, session)
        result = await session.scalars(select(ScheduledTask).where(ScheduledTask.tenant_id == principal.tenant_id, ScheduledTask.owner_id == principal.user_id, ScheduledTask.deleted_at.is_(None)))
        return [
            {"id": task.id, "name": task.name, "cron": task.cron, "taskType": task.task_type,
             "runAt": task.run_at.isoformat() if task.run_at else None, "maxRetries": task.max_retries,
             "status": task.status, "enabled": task.enabled,
             "lastRunAt": task.last_run_at.isoformat() if task.last_run_at else None,
             "last": task.last_run_at.isoformat() if task.last_run_at else None,
             "next": task.next_run_at.isoformat() if task.next_run_at else None,
             "timeZone": task.time_zone, "schedulePolicy": task.schedule_policy,
             "handlerType": (task.payload or {}).get("handlerType") or "",
             "canExecute": task.enabled and (task.payload or {}).get("handlerType") in task_contract.HANDLERS,
             "payload": task.payload or {}}
            for task in result
        ]


    @app.get("/api/AITasks/Capabilities")
    async def task_capabilities(principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)):
        await ensure_permission("task.read", principal, session)
        return await task_queue.task_capabilities()


    async def named_task(session, principal, name):
        rows = list(await session.scalars(select(ScheduledTask).where(
            ScheduledTask.tenant_id == principal.tenant_id, ScheduledTask.owner_id == principal.user_id,
            ScheduledTask.name == name, ScheduledTask.deleted_at.is_(None)).limit(2)))
        if len(rows) > 1:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "任务名称不唯一，请先更名"})
        return rows[0] if rows else None


    def normalize_run_at(value: object) -> datetime | None:
        if value in (None, ""):
            return None
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


    @app.post("/api/AITasks/AddEdit")
    async def task_add_edit(payload: TaskInput, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session), idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> bool:
        await ensure_permission("task.write", principal, session)
        if idempotency_key is not None and (
            payload.id is not None or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", idempotency_key)
        ):
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "创建幂等键无效，或被用于编辑"})
        await lock_tenant_writes(session, principal.tenant_id)
        extra = dict(payload.model_extra or {})
        if set(extra) - {"timeZone", "appId", "agentId", "description", "handler", "handlerType", "schedulePolicy",
                         "prompt", "maxSteps"}:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "unknown_task_input"})
        merged_payload = dict(payload.payload or {})
        for key in ("timeZone", "appId", "agentId", "description", "handler", "handlerType", "schedulePolicy",
                    "prompt", "maxSteps"):
            if extra.get(key) not in (None, ""):
                merged_payload[key] = extra[key]
        try:
            merged_payload = task_contract.normalize_input(merged_payload)
            time_zone = merged_payload.get("timeZone", "Asia/Shanghai")
            task_schedule.zone(time_zone)
            policy = merged_payload.get("schedulePolicy", "hold")
            if policy not in {"hold", "skip", "coalesce"}:
                raise ValueError("invalid_schedule_policy")
            run_at = task_schedule.normalize_once(payload.run_at, time_zone) if payload.task_type == "once" else None
            timestamp = datetime.now(UTC)
            if payload.task_type == "cron":
                task_schedule.parse(payload.cron)
            await ensure_permission("ai.chat.write" if merged_payload["handlerType"] == "agent" else "knowledge.read",
                                    principal, session)
            await task_contract.check_input(session, principal.tenant_id, principal.user_id, merged_payload)
            if not payload.name.strip():
                raise ValueError("empty_task_name")
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": str(exc)}) from exc
        create_data = {
            "name": payload.name, "cron": payload.cron, "taskType": payload.task_type,
            "runAt": run_at.isoformat() if run_at else None,
            "maxRetries": payload.max_retries, "payload": merged_payload,
        }
        if idempotency_key is not None:
            receipt = await session.scalar(select(Resource).where(
                Resource.tenant_id == principal.tenant_id, Resource.owner_id == principal.user_id,
                Resource.resource_type == "task_create_receipts", Resource.name == idempotency_key,
            ))
            if receipt is not None:
                original = receipt.data or {}
                previous = await session.get(ScheduledTask, original.get("taskId"))
                if original.get("input") != create_data or previous is None or (
                    previous.tenant_id != principal.tenant_id or previous.owner_id != principal.user_id
                    or previous.deleted_at is not None
                ):
                    raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "创建幂等键已用于其他内容或失效任务"})
                return True
        try:
            if run_at is not None and run_at <= timestamp:
                raise ValueError("once_requires_future_time")
            next_at = (run_at if run_at else task_schedule.next_time(payload.cron, time_zone, timestamp, policy))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": str(exc)}) from exc
        task = await session.get(ScheduledTask, payload.id) if payload.id else None
        if payload.id and task is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "任务不存在"})
        if task is not None and task.tenant_id != principal.tenant_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "任务不存在"})
        if task is not None and task.owner_id != principal.user_id:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "task not found"})
        if task is not None and task.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "任务不存在"})
        duplicates = await session.scalar(select(ScheduledTask.id).where(
            ScheduledTask.tenant_id == principal.tenant_id, ScheduledTask.owner_id == principal.user_id,
            ScheduledTask.name == payload.name.strip(), ScheduledTask.deleted_at.is_(None),
            ScheduledTask.id != (payload.id or 0)).limit(1))
        if duplicates:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "任务名称已存在"})
        if task is None:
            task = ScheduledTask(id=await new_id(session), tenant_id=principal.tenant_id, name=payload.name, cron=payload.cron, owner_id=principal.user_id)
            session.add(task)
            task.schedule_revision = 0
        else:
            running = await session.scalar(select(TaskRun.id).where(
                TaskRun.task_id == task.id, TaskRun.status.in_(["running", "uncertain"])).limit(1))
            if running:
                raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "运行中或结果不确定的任务需要先处理"})
            await task_contract.cancel_runs(session, task, reason="schedule_replaced")
        task.schedule_revision += 1
        task.name, task.cron = payload.name.strip(), payload.cron
        task.task_type, task.run_at = payload.task_type, run_at
        task.max_retries, task.payload, task.status = payload.max_retries, merged_payload, "ready"
        task.time_zone, task.schedule_policy, task.next_run_at, task.enabled = time_zone, policy, next_at, True
        # once 的意图保存与配置同事务；编辑撤销旧 receipt，持久版本挡住迟到消费。
        if task.task_type == "once":
            await task_contract.stage_run(session, task, scheduled_for=run_at)
            task.next_run_at = None
        if idempotency_key is not None:
            session.add(Resource(
                id=await new_id(session), tenant_id=principal.tenant_id, owner_id=principal.user_id,
                resource_type="task_create_receipts", name=idempotency_key,
                data={"input": create_data, "taskId": task.id},
            ))
        await session.commit()
        return True


    @app.delete("/api/AITasks/RemoveCronTask")
    async def task_remove(name: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("task.write", principal, session)
        await lock_tenant_writes(session, principal.tenant_id)
        task = await named_task(session, principal, name)
        if task is None:
            return False
        await task_contract.cancel_runs(session, task, reason="task_deleted")
        task.enabled, task.status, task.next_run_at, task.deleted_at = False, "cancelled", None, datetime.now(UTC)
        await session.commit()
        return True


    @app.get("/api/AITasks/TriggerCronTask")
    @app.post("/api/AITasks/Trigger")
    async def task_trigger(name: str, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await ensure_permission("task.write", principal, session)
        await lock_tenant_writes(session, principal.tenant_id)
        task = await named_task(session, principal, name)
        if task is None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "任务不存在"})
        if not task.enabled:
            raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "任务已取消或禁用"})
        try:
            normalized = task_contract.normalize_input(task.payload or {})
            await task_contract.check_input(session, task.tenant_id, task.owner_id, normalized)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail={"code": 400, "errMsg": "任务类型或绑定资源不可用"}) from exc
        capability = await task_queue.task_capabilities()
        if not capability["execution"]:
            raise HTTPException(status_code=503, detail={"code": 503, "errMsg": capability["reason"]})
        # 当前未完成的 run 重用持久 intent；重试不新建另一个收费/派发任务。
        existing = await session.scalar(select(OutboxEvent).join(
            TaskRun, TaskRun.id == OutboxEvent.entity_id,
        ).where(
            OutboxEvent.kind == "scheduled-task", OutboxEvent.tenant_id == principal.tenant_id,
            OutboxEvent.consume_status.in_(["pending", "running", "uncertain"]),
            TaskRun.task_id == task.id, TaskRun.tenant_id == principal.tenant_id,
            TaskRun.status.in_(["pending", "queued", "queue_unavailable", "running", "retrying", "uncertain"]),
        ).order_by(OutboxEvent.id.desc()).limit(1))
        if existing:
            event_id, run_id = existing.id, existing.entity_id
            if existing.consume_status == "uncertain":
                return {"queued": False, "jobId": existing.job_id, "runId": run_id,
                        "status": "uncertain", "automaticRetry": False}
        else:
            if task.task_type == "once":
                if not task.run_at:
                    raise HTTPException(status_code=409, detail={"code": 409, "errMsg": "一次性任务缺少计划"})
                # 同版本计划时刻的终态不可通过“触发”再次执行。
                run_id, event_id = await task_contract.stage_run(session, task, scheduled_for=task.run_at)
                run = await session.get(TaskRun, run_id)
                if run.status in task_contract.TERMINAL:
                    return {"queued": False, "runId": run.id, "status": run.status, "automaticRetry": False}
            else:
                run_id, event_id = await task_contract.stage_run(session, task)
        await session.commit()
        job_id = await outbox.dispatch_one(event_id)
        return {"queued": bool(job_id), "jobId": job_id, "runId": run_id,
                "status": "queued" if job_id else "queue_unavailable"}


    @app.get("/api/AITasks/{task_id}/Status")
    async def task_status(task_id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
        await ensure_permission("task.read", principal, session)
        task = await session.get(ScheduledTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id or task.owner_id != principal.user_id or task.deleted_at is not None:
            raise HTTPException(status_code=404, detail={"code": 404, "errMsg": "任务不存在"})
        runs = await session.scalars(select(TaskRun).where(TaskRun.tenant_id == principal.tenant_id, TaskRun.task_id == task.id).order_by(TaskRun.created_at.desc()).limit(20))
        return {
            "taskId": task.id, "name": task.name, "status": task.status,
            "runs": [{"id": run.id, "jobId": run.job_id, "status": run.status, "attempt": run.attempt, "result": run.result, "errorMessage": run.error_message, "createdAt": run.created_at.isoformat(), "finishedAt": run.finished_at.isoformat() if run.finished_at else None} for run in runs],
        }


    @app.post("/api/AITasks/{task_id}/Cancel")
    async def task_cancel(task_id: int, principal: Principal = Depends(current_principal), session: AsyncSession = Depends(get_session)) -> bool:
        await ensure_permission("task.write", principal, session)
        task = await session.get(ScheduledTask, task_id)
        if task is None or task.tenant_id != principal.tenant_id or task.owner_id != principal.user_id:
            return False
        await lock_tenant_writes(session, principal.tenant_id)
        await session.refresh(task)
        task.enabled, task.status, task.next_run_at = False, "cancelled", None
        await task_contract.cancel_runs(session, task)
        await session.commit()
        return True
    return SimpleNamespace(task_list=task_list, task_capabilities=task_capabilities, named_task=named_task, normalize_run_at=normalize_run_at, task_add_edit=task_add_edit, task_remove=task_remove, task_trigger=task_trigger, task_status=task_status, task_cancel=task_cancel)

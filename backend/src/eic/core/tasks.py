"""固定只读 handler、当前授权、持久计划与终态栅栏。"""
from datetime import UTC, datetime, timedelta
import hashlib
from sqlalchemy import exists, func, select, update
from eic.core.ids import new_id
from eic.core import outbox, task_schedule
from eic.models import (KnowledgeBase, KnowledgeDocument, Permission, Resource, Role,
                        RolePermission, ScheduledTask, TaskRun, Tenant, User, UserRole)

CONTRACT = "task-contract-v1"
HANDLERS = ("knowledge-check", "agent")
ACTIVE = ("pending", "queued", "queue_unavailable", "running", "retrying")
TERMINAL = ("success", "failed", "cancelled", "uncertain")

def worker_scope():
    from eic.core.config import get_settings
    return hashlib.sha256(get_settings().database_url.encode()).hexdigest()


def normalize_input(payload):
    data = dict(payload)
    if data.get("handler") is not None:
        if data.get("handlerType") not in (None, data["handler"]):
            raise ValueError("conflicting_task_handler")
        data["handlerType"] = data.pop("handler")
    kind = data.get("handlerType") or ("knowledge-check" if data.get("knowledge_base_id") else "")
    if kind not in HANDLERS:
        raise ValueError("handler_not_registered")
    if kind == "agent":
        if set(data) - {"handlerType", "appId", "agentId", "prompt", "maxSteps", "timeZone", "description", "schedulePolicy"}:
            raise ValueError("unknown_task_input")
        ids = [key for key in ("appId", "agentId") if data.get(key) is not None]
        if len(ids) != 1:
            raise ValueError("agent_application_required")
        value = data[ids[0]]
        if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**63:
            raise ValueError("invalid_app_id")
        prompt, steps = data.get("prompt"), data.get("maxSteps", 8)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16000:
            raise ValueError("invalid_agent_prompt")
        if type(steps) is not int or not 1 <= steps <= 32:
            raise ValueError("invalid_agent_max_steps")
        return {"handlerType": kind, ids[0]: int(value), "prompt": prompt, "maxSteps": steps,
                **{k: data[k] for k in ("timeZone", "description", "schedulePolicy") if k in data}}
    if data.get("appId") or data.get("agentId"):
        raise ValueError("unexpected_agent_reference")
    value = data.get("knowledge_base_id")
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**63:
        raise ValueError("invalid_knowledge_base_id")
    if set(data) - {"handlerType", "knowledge_base_id", "timeZone", "description", "appId", "agentId",
                    "schedulePolicy"}:
        raise ValueError("unknown_task_input")
    return {"handlerType": kind, "knowledge_base_id": int(value),
            **{k: data[k] for k in ("timeZone", "description", "schedulePolicy") if k in data}}


async def owner_authorized(session, task):
    user = await session.get(User, task.owner_id) if task.owner_id else None
    tenant = await session.get(Tenant, task.tenant_id)
    if (not user or user.tenant_id != task.tenant_id or not user.is_active
            or user.approval_status != "approved" or not tenant or not tenant.active):
        return False
    if user.is_super_admin:
        return True
    codes = set(await session.scalars(select(Permission.code)
        .join(RolePermission, RolePermission.permission_id == Permission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, Role.tenant_id == task.tenant_id)))
    required = {"task.write", "ai.chat.write" if (task.payload or {}).get("handlerType") == "agent" else "knowledge.read"}
    return required.issubset(codes) or "*" in codes


async def check_resources(session, tenant_id, payload):
    data = normalize_input(payload)
    if data["handlerType"] == "agent":
        from eic.core.task_agent import application_config
        return await application_config(session, tenant_id, data)
    return await check_base(session, tenant_id, data["knowledge_base_id"])


async def check_input(session, tenant_id, owner_id, normalized_payload):
    """保存和触发共用当前 owner 权限及执行资源校验，不产生外呼。"""
    from types import SimpleNamespace
    data = normalize_input(normalized_payload)
    task = SimpleNamespace(tenant_id=tenant_id, owner_id=owner_id, payload=data)
    if not await owner_authorized(session, task):
        raise ValueError("task_owner_permission_revoked")
    if data["handlerType"] == "agent":
        from eic.core.task_agent import load_context, make_tool_center
        await load_context(session, tenant_id, owner_id, data, make_tool_center())
    else:
        await check_resources(session, tenant_id, data)
    return data


async def check_base(session, tenant_id, base_id):
    resource = await session.get(Resource, base_id)
    base = await session.get(KnowledgeBase, base_id)
    if (not resource or not base or resource.tenant_id != tenant_id or base.tenant_id != tenant_id
            or resource.is_deleted or resource.resource_type != "knowledge_bases"
            or (resource.data or {}).get("enabled") is False):
        raise ValueError("knowledge_base_unavailable")
    return base


async def execute_handler(session, tenant_id, payload):
    data = normalize_input(payload)
    base = await check_base(session, tenant_id, data["knowledge_base_id"])
    rows = (await session.execute(select(KnowledgeDocument.index_status, func.count())
        .where(KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.knowledge_base_id == base.id,
               KnowledgeDocument.is_deleted.is_(False))
        .group_by(KnowledgeDocument.index_status))).all()
    # 产出是只读健康报告，不承诺索引/embedding/Agent执行，也不调用任何模型。
    return {"schemaVersion": 1, "handlerType": "knowledge-check", "status": "success",
            "report": {"knowledgeBaseId": str(base.id), "indexStatus": base.status,
                       "documentStates": {state: count for state, count in rows},
                       "documentCount": sum(count for _, count in rows),
                       "checkedAt": datetime.now(UTC).isoformat(), "readOnly": True}}


def valid_result(result, payload):
    if not isinstance(result, dict) or result.get("status") != "success":
        return False
    report = result.get("report")
    if (result.get("schemaVersion") != 1 or result.get("handlerType") != "knowledge-check"
            or not isinstance(report, dict) or report.get("readOnly") is not True
            or report.get("knowledgeBaseId") != str(payload.get("knowledge_base_id"))):
        return False
    states, total = report.get("documentStates"), report.get("documentCount")
    if (not isinstance(states, dict) or type(total) is not int or total < 0
            or any(not isinstance(k, str) or type(v) is not int or v < 0 for k, v in states.items())
            or sum(states.values()) != total or not isinstance(report.get("indexStatus"), str)):
        return False
    try:
        return datetime.fromisoformat(report["checkedAt"]).tzinfo is not None
    except (KeyError, ValueError, TypeError):
        return False


async def cancel_runs(session, task, *, reason="cancelled", run_id=None):
    runs = list(await session.scalars(select(TaskRun).where(
        TaskRun.task_id == task.id, TaskRun.tenant_id == task.tenant_id, TaskRun.status.in_(ACTIVE),
        *([TaskRun.id == run_id] if run_id is not None else []))))
    for run in runs:
        changed = await session.execute(update(TaskRun).where(
            TaskRun.id == run.id, TaskRun.status.in_(ACTIVE)
        ).values(status="cancelled", finished_at=func.current_timestamp(), error_message=reason,
                 result={"status": "cancelled", "sideEffectsReverted": False}))
        if changed.rowcount:
            await session.execute(update(outbox.OutboxEvent).where(
                outbox.OutboxEvent.kind == "scheduled-task", outbox.OutboxEvent.entity_id == run.id,
                outbox.OutboxEvent.tenant_id == task.tenant_id,
                outbox.OutboxEvent.consume_status.in_(["pending", "running"]),
            ).values(status="cancelled", consume_status="completed", owner_token=None,
                     lease_until=None, consume_token=None, consume_until=None,
                     result={"status": "cancelled"}))


async def stage_run(session, task, *, scheduled_for=None):
    if scheduled_for is not None:
        scheduled_for = task_schedule.utc(scheduled_for)
        key = f"schedule:{task.id}:{task.schedule_revision}:{scheduled_for.isoformat()}"
        existing = await session.scalar(select(outbox.OutboxEvent).where(
            outbox.OutboxEvent.tenant_id == task.tenant_id, outbox.OutboxEvent.event_key == key))
        if existing:
            return existing.entity_id, existing.id
    else:
        key = None
    run = TaskRun(id=await new_id(session), tenant_id=task.tenant_id, task_id=task.id,
                  schedule_revision=task.schedule_revision, scheduled_for=scheduled_for,
                  input_snapshot=dict(task.payload or {}), status="pending")
    session.add(run)
    await session.flush()
    event_id = await outbox.stage(session, "scheduled-task", run.id, task.tenant_id,
                                 event_key=key or f"task-run:{run.id}", available_at=scheduled_for)
    task.status = "pending"
    return run.id, event_id


async def scan_due(session, now):
    now = task_schedule.utc(now)
    await new_id(session)
    tasks = list(await session.scalars(select(ScheduledTask).where(
        ScheduledTask.enabled.is_(True), ScheduledTask.deleted_at.is_(None),
        ScheduledTask.next_run_at <= now,
        ~exists(select(TaskRun.id).where(
            TaskRun.task_id == ScheduledTask.id,
            TaskRun.status.in_((*ACTIVE, "uncertain")))),
    ).order_by(ScheduledTask.next_run_at, ScheduledTask.id).limit(100)))
    events = []
    for task in tasks:
        due = task_schedule.utc(task.next_run_at)
        # 未批准的旧计划与漏触发不补跑；可见 hold 等待明确配置。
        if task.schedule_policy == "hold" and now - due >= timedelta(minutes=1):
            task.status = "schedule_held"
            task.next_run_at = None
            continue
        if not await owner_authorized(session, task):
            task.enabled, task.status = False, "permission_revoked"
            continue
        try:
            normalize_input(task.payload or {})
            await check_input(session, task.tenant_id, task.owner_id, task.payload or {})
            next_due = (task_schedule.next_time(task.cron, task.time_zone, now, task.schedule_policy)
                        if task.task_type == "cron" else None)
        except ValueError:
            task.status = "schedule_held"
            task.next_run_at = None
            continue
        if task.schedule_policy == "skip" and now - due >= timedelta(minutes=1):
            task.next_run_at = next_due
            task.status = "missed"
            continue
        _, event = await stage_run(session, task, scheduled_for=due)
        events.append(event)
        task.next_run_at = next_due
    return events

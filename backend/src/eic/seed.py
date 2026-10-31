from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from eic.ai.prompt_catalog import BY_NAME, CATALOG
from eic.ai.prompt_templates import ensure_builtin
from eic.core.config import get_settings
from eic.core.database import SessionLocal
from eic.core.ids import new_id as _id
from eic.core.security import hash_password
from eic.knowledge.documents import parent_child_chunks
from eic.models import (
    AIApp,
    AIModel,
    AIPrompt,
    Department,
    KnowledgeBase,
    KnowledgeChunk,
    KnowledgeDocument,
    KnowledgeIngestionRun,
    Position,
    Resource,
    ScheduledTask,
    Tenant,
    User,
)

DEMO_DOCUMENTS = (
    {
        "name": "企业治理与 AI 使用规范.md",
        "source_url": "https://www.nist.gov/itl/ai-risk-management-framework",
        "content": """# 企业治理与 AI 使用规范

本示例文档根据 NIST AI Risk Management Framework 的公开主题整理为企业内部知识库样例，不复制原文。

## 目标
企业使用 AI 系统时，应明确业务目标、责任人、数据边界、风险等级和人工复核机制。模型输出需要保留来源和审计记录，重要业务决定不能只依据未经复核的模型回答。

## 运行要求
模型上线前应完成 Endpoint、模型名、模型类型、密钥状态和备用模型配置检查。涉及个人信息、客户资料或商业秘密的请求，需要经过数据分类和最小权限校验。

## 复核流程
发现答案缺少证据、与制度冲突或输出包含敏感信息时，系统应停止自动执行，记录请求 ID，并转交业务负责人复核。
""",
    },
    {
        "name": "应用安全验收规范.md",
        "source_url": "https://owasp.org/www-project-application-security-verification-standard/",
        "content": """# 应用安全验收规范

本示例文档参考 OWASP Application Security Verification Standard 的公开安全主题整理。

## 身份与访问控制
接口必须校验 JWT、租户和用户状态。所有资源查询都要带租户条件，角色权限需要在服务端重新计算，不能只依赖前端菜单隐藏。

## 敏感配置
API Key、Authorization Header 和模型密钥只能写入加密字段。日志仅保留 Provider、模型名、Endpoint、请求 ID 和结果状态。

## 文件与接口
上传文件需要校验扩展名、大小、路径和租户目录。下载接口必须验证文件归属。外部 HTTP 调用需要设置超时、响应大小上限和错误脱敏。
""",
    },
    {
        "name": "企业知识库问答流程.md",
        "source_url": "local-synthetic",
        "content": """# 企业知识库问答流程

## 提问流程
用户提交问题后，系统先加载 AI 应用配置和最近聊天历史，再根据知识库执行查询规范化、BM25 检索、向量检索、RRF 融合、Rerank 和父块回填。

## 回答规则
有证据时优先引用知识库内容；证据不足时明确说明依据不足，不编造制度条款。需要执行工具时，先检查 Skill 白名单、工具权限、参数模式和租户边界。

## 记录内容
系统保存请求 ID、检索证据、模型名称、Token 估算、工具审计和最终答案。用户停止生成或浏览器断开时，聊天状态需要进入可识别的取消或失败状态。
""",
    },
    {
        "name": "采购申请流程.md",
        "source_url": "local-synthetic",
        "content": """# 采购申请流程

员工提交采购申请时，应填写申请部门、用途、预算金额、期望到货时间和供应商信息。部门负责人先确认业务必要性，财务人员复核预算，超过授权额度的申请进入更高级别审批。

知识库助手可以解释流程、检查字段是否完整和提示所需审批级别，但不能替代最终审批。所有审批动作应保留操作者、时间、结果和关联申请编号。
""",
    },
)



async def _dedupe_catalog_resources(session, tenant) -> None:
    rows = list(await session.scalars(select(Resource).where(
        Resource.tenant_id == tenant.id,
        Resource.is_deleted.is_(False),
        Resource.resource_type.in_(("ai_apps", "knowledge_bases", "skills", "messages")),
    )))
    grouped: dict[tuple[str, str], list[Resource]] = {}
    for row in rows:
        key = (row.resource_type, (row.name or "").strip())
        grouped.setdefault(key, []).append(row)
    for items in grouped.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda item: ((item.created_at.isoformat() if item.created_at else ""), item.id))
        for extra in items[1:]:
            extra.is_deleted = True


async def seed_demo_data() -> dict[str, int | str]:
    """Create deterministic-shaped, tenant-bound demo content exactly once."""
    settings = get_settings()
    if not settings.seed_demo_data or str(settings.environment).lower() not in {"development", "test", "testing", "simulation"}:
        return {"status": "seed_disabled", "documents": 0}
    async with SessionLocal() as session:
        async with session.begin():
            # 使用共享分配器的事务行锁，在读取标记前串行化 seed；提交或回滚时释放。
            await _id(session)
            tenant = await session.scalar(select(Tenant).where(Tenant.code == 1000))
            admin = await session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == settings.bootstrap_admin)) if tenant else None
            if tenant is None or admin is None:
                return {"status": "waiting_for_admin", "documents": 0}
            marker = await session.scalar(
                select(Resource).where(
                    Resource.tenant_id == tenant.id,
                    Resource.resource_type == "demo_seed",
                    Resource.name == "enterprise-core-v1",
                    Resource.is_deleted.is_(False),
                )
            )
            if marker is not None:
                return {"status": "already_seeded", "documents": len(DEMO_DOCUMENTS)}

            department = await session.scalar(select(Department).where(Department.tenant_id == tenant.id, Department.name == "企业智能中心"))
            if department is None:
                department = Department(id=await _id(session), tenant_id=tenant.id, name="企业智能中心")
                session.add(department)
            position = await session.scalar(select(Position).where(Position.tenant_id == tenant.id, Position.name == "知识运营专员"))
            if position is None:
                session.add(Position(id=await _id(session), tenant_id=tenant.id, name="知识运营专员", description="维护知识库和问答质量"))
            if settings.demo_user_password:
                demo_user = await session.scalar(select(User).where(User.tenant_id == tenant.id, User.username == "demo"))
                if demo_user is None:
                    session.add(User(id=await _id(session), tenant_id=tenant.id, username="demo", nickname="演示用户", password_hash=hash_password(settings.demo_user_password), is_active=True))

            default_prompt = await ensure_builtin(session, tenant, admin, BY_NAME["企业知识助手默认 Prompt"])
            prompt_id = default_prompt.id

            knowledge_base_id = await _id(session)
            session.add(KnowledgeBase(id=knowledge_base_id, tenant_id=tenant.id, name="通用企业管理知识库", chunk_size=1200, chunk_overlap=180, status="ready"))
            session.add(Resource(id=knowledge_base_id, tenant_id=tenant.id, resource_type="knowledge_bases", name="通用企业管理知识库", owner_id=admin.id, data={"description": "组织制度、流程规范和企业内部助手示例", "source": "public-derived-and-synthetic", "promptId": prompt_id}))

            app_id = await _id(session)
            app_data = {
                "promptId": prompt_id,
                "knowledgeBaseId": knowledge_base_id,
                "status": "needs_model_config",
                "source": "seed",
                "describe": "通用企业管理问答示例",
                "description": "通用企业管理问答示例",
                "type": "chat",
            }
            session.add(AIApp(id=app_id, tenant_id=tenant.id, name="企业内部知识助手", description="通用企业管理问答示例", prompt_id=prompt_id, knowledge_base_id=knowledge_base_id, config=app_data))
            session.add(Resource(id=app_id, tenant_id=tenant.id, resource_type="ai_apps", name="企业内部知识助手", owner_id=admin.id, data=app_data))

            for item in DEMO_DOCUMENTS:
                document_id = await _id(session)
                content = item["content"]
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                session.add(KnowledgeDocument(id=document_id, tenant_id=tenant.id, knowledge_base_id=knowledge_base_id, source_name=item["name"], content=content, status="ready"))
                # Flush the parent row before inserting chunks so MySQL can satisfy
                # the document foreign key within the same transaction.
                await session.flush()
                drafts = parent_child_chunks(content, parent_size=1600, child_size=700, child_overlap=100)
                parent_ids: dict[int, int] = {}
                for draft in drafts:
                    parent_ids.setdefault(draft.parent_index, await _id(session))
                for parent_index, parent_id in parent_ids.items():
                    parent_content = next(d.parent_content for d in drafts if d.parent_index == parent_index)
                    session.add(KnowledgeChunk(id=parent_id, tenant_id=tenant.id, knowledge_base_id=knowledge_base_id, document_id=document_id, parent_chunk_id=None, chunk_index=-(parent_index + 1), content=parent_content, content_hash=hashlib.sha256(parent_content.encode("utf-8")).hexdigest(), attributes={"chunk_kind": "parent", "source_name": item["name"], "source_url": item["source_url"]}))
                for child_index, draft in enumerate(drafts):
                    session.add(KnowledgeChunk(id=await _id(session), tenant_id=tenant.id, knowledge_base_id=knowledge_base_id, document_id=document_id, parent_chunk_id=parent_ids[draft.parent_index], chunk_index=child_index, content=draft.content, content_hash=hashlib.sha256(draft.content.encode("utf-8")).hexdigest(), attributes={"chunk_kind": "child", "source_name": item["name"], "source_url": item["source_url"], "content_hash": content_hash}))
                session.add(KnowledgeIngestionRun(id=await _id(session), tenant_id=tenant.id, document_id=document_id, status="seeded_pending_embedding", chunk_count=len(drafts), embedding_count=0))

            session.add(ScheduledTask(id=await _id(session), tenant_id=tenant.id, name="知识库每日检查", cron="0 2 * * *", task_type="cron", payload={"handlerType": "knowledge-check", "knowledge_base_id": knowledge_base_id}, status="disabled", enabled=False, owner_id=admin.id))
            session.add(Resource(id=await _id(session), tenant_id=tenant.id, resource_type="demo_seed", name="enterprise-core-v1", owner_id=admin.id, data={"documents": len(DEMO_DOCUMENTS), "source": "public-derived-and-synthetic"}))
            await _backfill_catalog(session, tenant, admin)
            return {"status": "seeded", "documents": len(DEMO_DOCUMENTS), "knowledge_base_id": knowledge_base_id}


async def _backfill_ai_app_display_fields(session, tenant) -> None:
    apps = await session.scalars(
        select(Resource).where(
            Resource.tenant_id == tenant.id,
            Resource.resource_type == "ai_apps",
            Resource.is_deleted.is_(False),
        )
    )
    for item in apps:
        data = dict(item.data or {})
        ai_app = await session.get(AIApp, item.id)
        app_description = str(getattr(ai_app, "description", "") or "").strip()
        current = str(data.get("describe") or data.get("description") or "").strip()
        name = str(item.name or "").strip()
        description = app_description or current or name
        if app_description and (not current or current == name):
            description = app_description
        changed = False
        if description and data.get("describe") != description:
            data["describe"] = description
            changed = True
        if description and data.get("description") != description:
            data["description"] = description
            changed = True
        if not data.get("type"):
            data["type"] = "chat"
            changed = True
        if changed:
            item.data = data
        if ai_app is not None and not ai_app.description and description:
            ai_app.description = description
        chat_model = await session.scalar(
            select(AIModel).where(AIModel.tenant_id == tenant.id, AIModel.model_type == "chat", AIModel.enabled.is_(True)).order_by(AIModel.updated_at.desc())
        )
        if chat_model is not None:
            if ai_app is not None and not ai_app.chat_model_id:
                ai_app.chat_model_id = chat_model.id
            if not data.get("chatModelID") and not data.get("chatModelId"):
                data["chatModelID"] = chat_model.id
                data["chatModelId"] = chat_model.id
                changed = True
            if data.get("status") == "needs_model_config":
                data["status"] = "ready"
                changed = True
            if changed:
                item.data = data


async def _ensure_demo_knowledge_documents(session, tenant) -> None:
    bases = list(await session.scalars(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id)))
    if not bases:
        return
    for knowledge_base in bases:
        existing = await session.scalar(
            select(func.count()).select_from(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant.id,
                KnowledgeDocument.knowledge_base_id == knowledge_base.id,
            )
        ) or 0
        if existing:
            if knowledge_base.status in (None, "", "unknown"):
                knowledge_base.status = "ready"
            continue
        for item in DEMO_DOCUMENTS:
            document_id = await _id(session)
            content = item["content"]
            session.add(KnowledgeDocument(
                id=document_id, tenant_id=tenant.id, knowledge_base_id=knowledge_base.id,
                source_name=item["name"], content=content, status="ready",
            ))
            await session.flush()
            drafts = parent_child_chunks(content, parent_size=1600, child_size=700, child_overlap=100)
            parent_ids: dict[int, int] = {}
            for draft in drafts:
                parent_ids.setdefault(draft.parent_index, await _id(session))
            for parent_index, parent_id in parent_ids.items():
                parent_content = next(d.parent_content for d in drafts if d.parent_index == parent_index)
                session.add(KnowledgeChunk(
                    id=parent_id, tenant_id=tenant.id, knowledge_base_id=knowledge_base.id, document_id=document_id,
                    parent_chunk_id=None, chunk_index=-(parent_index + 1), content=parent_content,
                    content_hash=hashlib.sha256(parent_content.encode("utf-8")).hexdigest(),
                    attributes={"chunk_kind": "parent", "source_name": item["name"], "source_url": item["source_url"]},
                ))
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            for child_index, draft in enumerate(drafts):
                session.add(KnowledgeChunk(
                    id=await _id(session), tenant_id=tenant.id, knowledge_base_id=knowledge_base.id, document_id=document_id,
                    parent_chunk_id=parent_ids[draft.parent_index], chunk_index=child_index, content=draft.content,
                    content_hash=hashlib.sha256(draft.content.encode("utf-8")).hexdigest(),
                    attributes={"chunk_kind": "child", "source_name": item["name"], "source_url": item["source_url"], "content_hash": content_hash},
                ))
            session.add(KnowledgeIngestionRun(
                id=await _id(session), tenant_id=tenant.id, document_id=document_id, status="seeded_pending_embedding",
                chunk_count=len(drafts), embedding_count=0,
            ))
        knowledge_base.status = "ready"


async def _ensure_named(session, model, tenant, name, factory):
    item = await session.scalar(select(model).where(model.tenant_id == tenant.id, model.name == name, model.active.is_(True)))
    if item is None:
        item = factory(await _id(session))
        session.add(item)
        await session.flush()
    return item


async def _rewrite_department_refs(session, keep, extra) -> None:
    children = await session.scalars(select(Department).where(Department.parent_id == extra.id))
    for child in children:
        child.parent_id = keep.id
    users = await session.scalars(select(User).where(User.department_id == extra.id))
    for user in users:
        user.department_id = keep.id


async def _rewrite_position_refs(session, keep, extra) -> None:
    users = await session.scalars(select(User).where(User.position_id == extra.id))
    for user in users:
        user.position_id = keep.id


async def _dedupe_named(session, model, tenant, rewriter=None) -> None:
    rows = list(await session.scalars(select(model).where(model.tenant_id == tenant.id, model.active.is_(True))))
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault((row.name or "").strip(), []).append(row)
    for name, items in grouped.items():
        if not name or len(items) < 2:
            continue
        items.sort(key=lambda item: ((item.created_at.isoformat() if getattr(item, "created_at", None) else ""), item.id))
        keep = items[0]
        for extra in items[1:]:
            extra.active = False
            if rewriter is not None:
                await rewriter(session, keep, extra)


async def _ensure_resource(session, tenant, admin, resource_type, name, data):
    item = await session.scalar(
        select(Resource).where(
            Resource.tenant_id == tenant.id,
            Resource.resource_type == resource_type,
            Resource.name == name,
            Resource.is_deleted.is_(False),
        )
    )
    if item is None:
        item = Resource(id=await _id(session), tenant_id=tenant.id, resource_type=resource_type, name=name, owner_id=admin.id, data=data)
        session.add(item)
        return item
    merged = dict(item.data or {})
    changed = False
    for key, value in data.items():
        if key not in merged or merged.get(key) in (None, "", []):
            merged[key] = value
            changed = True
    if changed:
        item.data = merged
    return item


async def _backfill_catalog(session, tenant, admin) -> None:
    """Idempotent empty-state catalog: org tree, public-style prompts, skills, announcements."""
    hq = await _ensure_named(
        session, Department, tenant, "企智枢总部",
        lambda allocated_id: Department(id=allocated_id, tenant_id=tenant.id, name="企智枢总部", parent_id=None, active=True, description="企智枢集团总部，统管各部门协同"),
    )
    if not (getattr(hq, "description", "") or "").strip():
        hq.description = "企智枢集团总部，统管各部门协同"
    departments = [
        ("企业管理中心", "统筹制度、流程与跨部门协同"),
        ("技术研发部", "负责平台研发、模型接入与知识检索"),
        ("人力资源部", "组织、岗位与人员配置"),
        ("财务部", "预算、报销与成本核算"),
        ("运营支持部", "日常运营、公告与用户支持"),
    ]
    for name, description in departments:
        item = await _ensure_named(
            session, Department, tenant, name,
            lambda allocated_id, name=name, description=description: Department(id=allocated_id, tenant_id=tenant.id, name=name, parent_id=hq.id, active=True, description=description),
        )
        if hq is not None and not item.parent_id:
            item.parent_id = hq.id
        if not (getattr(item, "description", "") or "").strip():
            item.description = description
    smart = await session.scalar(select(Department).where(Department.tenant_id == tenant.id, Department.name == "企业智能中心"))
    if smart is not None and hq is not None and smart.id != hq.id and not smart.parent_id:
        smart.parent_id = hq.id
    if smart is not None and not (getattr(smart, "description", "") or "").strip():
        smart.description = "企业智能中台，负责模型、知识库与智能体运营"

    positions = [
        ("系统管理员", "维护租户、权限与系统配置"),
        ("知识运营专员", "维护知识库、提示词与问答质量"),
        ("研发工程师", "负责智能体、模型接入与检索链路"),
        ("产品经理", "规划后台功能与验收标准"),
        ("人力资源专员", "维护组织架构与岗位编制"),
        ("财务专员", "处理对公结算与费用审核"),
        ("运营专员", "处理公告、工单与日常运营"),
    ]
    for name, description in positions:
        await _ensure_named(
            session, Position, tenant, name,
            lambda allocated_id, name=name, description=description: Position(id=allocated_id, tenant_id=tenant.id, name=name, description=description, active=True),
        )

    for item in CATALOG:
        if item["kind"] == "optimized":
            await ensure_builtin(session, tenant, admin, item)

    skills = [
        {"name": "calculator", "classMethod": "add", "description": "基础数值计算，用于金额、数量核对", "skillToolType": 1, "activeStatus": 1},
        {"name": "knowledge_lookup", "classMethod": "search", "description": "按关键词检索内部知识库摘要", "skillToolType": 1, "activeStatus": 1},
        {"name": "datetime_helper", "classMethod": "now", "description": "输出当前时间与工作日提醒", "skillToolType": 1, "activeStatus": 1},
        {"name": "json_pretty", "classMethod": "format", "description": "格式化 JSON 文本", "skillToolType": 1, "activeStatus": 1},
        {"name": "text_outline", "classMethod": "outline", "description": "把长文本整理成提纲", "skillToolType": 1, "activeStatus": 1},
    ]
    for item in skills:
        await _ensure_resource(session, tenant, admin, "skills", item["name"], item)

    knowledge_bases = [
        ("企业制度知识库", "考勤、报销、权限与安全制度摘要，供内部问答引用。"),
        ("公开框架知识库", "整理自公开安全与治理框架的企业落地说明。"),
    ]
    for name, description in knowledge_bases:
        existing = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.name == name))
        if existing is None:
            kid = await _id(session)
            session.add(KnowledgeBase(id=kid, tenant_id=tenant.id, name=name, chunk_size=800, chunk_overlap=120, status="ready"))
            session.add(Resource(id=kid, tenant_id=tenant.id, resource_type="knowledge_bases", name=name, owner_id=admin.id, data={"description": description, "source": "public-style-seed", "status": "ready"}))
        else:
            await _ensure_resource(session, tenant, admin, "knowledge_bases", name, {"description": description, "source": "public-style-seed", "status": "ready"})

    messages = [
        ("欢迎使用企智枢AI后台管理系统", "请先在模型配置中绑定可用对话模型，再打开智能体管理把模型绑定后即可真实 Chat。", "announcement"),
        ("组织架构已预置部门", "部门和岗位已在组织架构中建立。新增用户时可选手机、邮箱、部门、岗位和头像，写入用户表。", "announcement"),
        ("权限模块名称已中文化", "角色权限树按用户、模型、知识库等模块分组，不再只显示英文编码。", "system"),
        ("系统维护通知", "日志、字典和个人中心已接入当前租户数据，可在对应菜单中查看。", "private-system"),
        ("待办：完善个人资料", "请在个人中心上传头像并确认部门、岗位、手机和邮箱。", "private-user"),
        ("智能体使用提示", "对话前请确认智能体已绑定 Chat 模型，一键测试通过后再发起会话。", "ai-user"),
    ]
    for title, content, kind in messages:
        await _ensure_resource(
            session, tenant, admin, "messages", title,
            {
                "title": title,
                "content": content,
                "messageContent": content,
                "type": kind,
                "sysMsgType": kind,
                "createUser": admin.username,
                "sendUserName": admin.username,
                "isRead": False,
            },
        )

    dictionaries = [
        ("user_status", "1", "启用", "用户启用状态"),
        ("user_status", "0", "停用", "用户停用状态"),
        ("sex", "1", "男", "性别"),
        ("sex", "2", "女", "性别"),
        ("permission_type", "4", "接口权限", "权限类型"),
    ]
    for dict_type, key, value, remarks in dictionaries:
        await _ensure_resource(
            session, tenant, admin, "dictionaries", f"{dict_type}:{key}",
            {"type": dict_type, "key": key, "value": value, "remarks": remarks, "isSystem": "是", "sort": 0},
        )

    await _ensure_resource(
        session, tenant, admin, "http_logs", "登录",
        {
            "userName": admin.username, "operateType": "登录", "operateRemark": "seed login",
            "ip": "127.0.0.1", "device": "seed", "httpUrl": "/api/Authorize/GetToken",
            "httpBody": "", "httpMethod": "POST", "httpAction": "/api/Authorize/GetToken",
            "createTime": "2026-09-11T00:00:00+00:00",
        },
    )
    await _ensure_resource(
        session, tenant, admin, "os_logs", "登录",
        {
            "createTime": "2026-09-11T00:00:00+00:00", "table": "users", "tableId": admin.id,
            "sign": "登录", "content": "seed os log", "actionUserName": admin.username,
            "ipAddress": "127.0.0.1", "deviceMark": "seed",
        },
    )

    await _dedupe_named(session, Department, tenant, _rewrite_department_refs)
    await _dedupe_named(session, Position, tenant, _rewrite_position_refs)
    await _ensure_expanded_ai_catalog(session, tenant, admin)

    if not getattr(admin, "phone", None):
        admin.phone = "13800001000"
    if not getattr(admin, "email", None):
        admin.email = "admin@enterprise.local"
    admin.department_id = admin.department_id or hq.id
    sys_pos = await session.scalar(select(Position).where(Position.tenant_id == tenant.id, Position.name == "系统管理员"))
    if sys_pos is not None and not admin.position_id:
        admin.position_id = sys_pos.id



async def _ensure_expanded_ai_catalog(session, tenant, admin) -> None:
    """Idempotent catalog expansion: agents/prompts/kbs/skills with Chinese copy."""
    await _dedupe_catalog_resources(session, tenant)
    for item in CATALOG:
        await ensure_builtin(session, tenant, admin, item)

    skill_renames = {
        "calculator": ("数值计算", "基础数值计算，用于金额、数量核对"),
        "knowledge_lookup": ("知识检索", "按关键词检索内部知识库摘要"),
        "datetime_helper": ("工作日时间", "输出当前时间与工作日提醒"),
        "json_pretty": ("JSON格式化", "格式化 JSON 文本，便于核对接口报文"),
        "text_outline": ("文本提纲", "把长文本整理成层级提纲"),
    }
    for old_name, (new_name, description) in skill_renames.items():
        item = await session.scalar(select(Resource).where(
            Resource.tenant_id == tenant.id, Resource.resource_type == "skills",
            Resource.name == old_name, Resource.is_deleted.is_(False),
        ))
        taken = await session.scalar(select(Resource).where(
            Resource.tenant_id == tenant.id, Resource.resource_type == "skills",
            Resource.name == new_name, Resource.is_deleted.is_(False),
        ))
        if item is not None and taken is None:
            item.name = new_name
            data = dict(item.data or {})
            data["name"] = new_name
            data["label"] = new_name
            data["description"] = description
            data["describe"] = description
            item.data = data
        elif item is not None:
            item.is_deleted = True

    extra_skills = [
        {"name": "文本摘要", "classMethod": "summarize", "description": "把长文本压缩成关键结论和待办", "skillToolType": 1, "activeStatus": 1, "label": "文本摘要"},
        {"name": "清单核对", "classMethod": "checklist", "description": "对照清单检查字段是否完整", "skillToolType": 1, "activeStatus": 1, "label": "清单核对"},
        {"name": "风险提示", "classMethod": "risk", "description": "从描述中提取合规、安全和进度风险", "skillToolType": 1, "activeStatus": 1, "label": "风险提示"},
        {"name": "术语解释", "classMethod": "glossary", "description": "用企业内部语境解释专业术语", "skillToolType": 1, "activeStatus": 1, "label": "术语解释"},
        {"name": "待办拆解", "classMethod": "todos", "description": "把目标拆成可执行待办、负责人和截止时间", "skillToolType": 1, "activeStatus": 1, "label": "待办拆解"},
        {"name": "会议纪要整理", "classMethod": "meeting_notes", "description": "Skill 范例：把会议记录整理成决议、待办和责任人", "skillToolType": 2, "activeStatus": 1, "label": "会议纪要整理"},
        {"name": "制度条款对照", "classMethod": "policy_compare", "description": "Skill 范例：对照制度条款检查材料是否覆盖关键要求", "skillToolType": 2, "activeStatus": 1, "label": "制度条款对照"},
        {"name": "周报生成", "classMethod": "weekly_report", "description": "Skill 范例：把本周进展、风险和下周计划整理成周报", "skillToolType": 2, "activeStatus": 1, "label": "周报生成"},
        {"name": "企业内部知识检索 MCP", "classMethod": "kb_search", "description": "MCP 范例：按关键词检索内部知识库，示例地址未接通生产服务", "skillToolType": 3, "activeStatus": 1, "label": "企业内部知识检索 MCP", "mcpUrl": "https://mcp.example.local/knowledge", "mcpType": "https"},
        {"name": "工单查询 MCP", "classMethod": "ticket_lookup", "description": "MCP 范例：按工单号查询处理进度，示例地址未接通生产服务", "skillToolType": 3, "activeStatus": 1, "label": "工单查询 MCP", "mcpUrl": "https://mcp.example.local/tickets/sse", "mcpType": "sse"},
    ]
    for item in extra_skills:
        resource = await _ensure_resource(session, tenant, admin, "skills", item["name"], item)
        data = dict(resource.data or {})
        data.update(item)
        resource.data = data

    extra_kbs = [
        ("人力资源管理知识库", "考勤、入职、岗位编制与请假流程摘要。"),
        ("财务管理知识库", "报销、预算、对公结算与发票核对说明。"),
        ("信息安全知识库", "账号权限、数据分级和安全事件上报要点。"),
        ("采购与供应商知识库", "采购申请、审批级别和供应商信息字段。"),
        ("客户服务知识库", "工单分类、升级路径和回复口径示例。"),
        ("项目管理知识库", "立项、里程碑、风险登记和复盘模板。"),
        ("办公协同知识库", "会议、公告、文档流转和跨部门协同约定。"),
        ("数据治理知识库", "主数据、报表口径和数据质量检查项。"),
    ]
    for name, description in extra_kbs:
        existing = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.name == name))
        if existing is None:
            kid = await _id(session)
            session.add(KnowledgeBase(id=kid, tenant_id=tenant.id, name=name, chunk_size=800, chunk_overlap=120, status="ready"))
            session.add(Resource(id=kid, tenant_id=tenant.id, resource_type="knowledge_bases", name=name, owner_id=admin.id, data={"description": description, "describe": description, "source": "public-style-seed", "status": "ready"}))
        else:
            await _ensure_resource(session, tenant, admin, "knowledge_bases", name, {"description": description, "describe": description, "source": "public-style-seed", "status": "ready"})

    chat_model = await session.scalar(
        select(AIModel).where(AIModel.tenant_id == tenant.id, AIModel.model_type == "chat", AIModel.enabled.is_(True)).order_by(AIModel.updated_at.desc())
    )
    default_kb = await session.scalar(select(KnowledgeBase).where(KnowledgeBase.tenant_id == tenant.id, KnowledgeBase.name == "通用企业管理知识库"))
    extra_apps = [
        ("会议纪要助手", "把会议记录整理成结论和待办", "会议纪要整理"),
        ("工单分类助手", "判断工单类型、紧急程度和建议处理部门", "工单分类助手"),
        ("合规审查助手", "检查文本中的越权承诺、密钥和个人信息风险", "合规审查助手"),
        ("客服回复助手", "把内部草稿改写成可对客发送的回复", "客服回复润色"),
    ]
    for name, description, prompt_name in extra_apps:
        prompt = await session.scalar(select(AIPrompt).where(AIPrompt.tenant_id == tenant.id, AIPrompt.name == prompt_name))
        existing_app = await session.scalar(select(AIApp).where(AIApp.tenant_id == tenant.id, AIApp.name == name))
        data = {
            "describe": description,
            "description": description,
            "type": "chat",
            "source": "public-style-seed",
            "status": "ready" if chat_model is not None else "needs_model_config",
        }
        if prompt is not None:
            data["promptId"] = prompt.id
        if default_kb is not None:
            data["knowledgeBaseId"] = default_kb.id
        if chat_model is not None:
            data["chatModelID"] = chat_model.id
            data["chatModelId"] = chat_model.id
        if existing_app is None:
            app_id = await _id(session)
            session.add(AIApp(
                id=app_id, tenant_id=tenant.id, name=name, description=description,
                prompt_id=getattr(prompt, "id", None), knowledge_base_id=getattr(default_kb, "id", None),
                chat_model_id=getattr(chat_model, "id", None), config=data, enabled=True,
            ))
            session.add(Resource(id=app_id, tenant_id=tenant.id, resource_type="ai_apps", name=name, owner_id=admin.id, data=data))
        else:
            if not existing_app.description:
                existing_app.description = description
            if chat_model is not None and not existing_app.chat_model_id:
                existing_app.chat_model_id = chat_model.id
            if prompt is not None and not existing_app.prompt_id:
                existing_app.prompt_id = prompt.id
            await _ensure_resource(session, tenant, admin, "ai_apps", name, data)
    await _dedupe_catalog_resources(session, tenant)

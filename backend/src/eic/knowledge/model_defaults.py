"""默认模型只用于未绑定知识库；已有向量空间不随默认值变动。"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from eic.models import AIModel, KnowledgeDocument, TenantModelDefaults


async def validated_model(session, tenant_id, value, kind):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, bool) or not str(value).isdigit() or not 0 < int(value) < 2**63:
        raise HTTPException(400, detail={"errMsg": "模型 ID 无效"})
    model = await session.get(AIModel, int(value))
    if not model or model.tenant_id != tenant_id or model.model_type != kind or not model.enabled:
        raise HTTPException(400, detail={"errMsg": "默认模型不存在、已停用或类型不匹配"})
    return model.id


async def bind_missing_defaults(session, base, *, require_embedding=False, require_rerank=False, persist=False,
                                allow_unconfigured=False):
    def assign(name, value):
        if persist:
            setattr(base, name, value)
        else:
            # 只读检索不得持有写事务跨越外呼；入库建立版本时显式持久绑定。
            set_committed_value(base, name, value)
    defaults = await session.get(TenantModelDefaults, base.tenant_id)
    if require_embedding and (not base.embedding_model_id or base.inherits_embedding_model):
        active = await session.scalar(select(KnowledgeDocument.id).where(
            KnowledgeDocument.tenant_id == base.tenant_id,
            KnowledgeDocument.knowledge_base_id == base.id,
            KnowledgeDocument.is_deleted.is_(False),
            KnowledgeDocument.active_index_version.is_not(None)).limit(1))
        if active and not base.embedding_model_id and require_embedding and not allow_unconfigured:
            raise HTTPException(409, detail={"errMsg": "已有索引缺少向量模型绑定，请确认原模型并显式配置，不能自动替换"})
        if not active:
            assign("embedding_model_id", await validated_model(session, base.tenant_id, defaults.embedding_model_id if defaults else None, "embedding"))
            assign("inherits_embedding_model", True)
    if require_rerank and (not base.rerank_model_id or base.inherits_rerank_model):
        assign("rerank_model_id", await validated_model(session, base.tenant_id, defaults.rerank_model_id if defaults else None, "rerank"))
        assign("inherits_rerank_model", True)
    for attribute, kind, required in (("embedding_model_id", "embedding", require_embedding),
                                      ("rerank_model_id", "rerank", require_rerank)):
        value = getattr(base, attribute)
        if required and not value and not allow_unconfigured:
            raise HTTPException(400, detail={"errMsg": f"请先配置知识库或租户默认 {kind} 模型"})
        if required and value:
            await validated_model(session, base.tenant_id, value, kind)

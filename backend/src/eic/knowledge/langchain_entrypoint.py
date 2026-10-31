"""标准检索接口只负责委托；EIC 服务仍负责身份、范围、版本和引用。"""
from typing import Any


async def retrieve_knowledge(
    session, *, tenant_id: int, base, query: str,
    extra: dict[str, Any] | None = None,
    filters: dict[str, object] | None = None,
    limit: int | None = None, before_external=None,
) -> dict[str, Any]:
    """与原公开检索同签名；调用前范围来自服务参数，非 Document 元数据。"""
    from eic.knowledge.ops import retrieve_knowledge as domain_retrieve

    # 领域公开入口现在启动子图，授权/召回/融合/重排均在图节点中执行。
    return await domain_retrieve(
        session, tenant_id=tenant_id, base=base, query=query,
        extra=extra, filters=filters, limit=limit, before_external=before_external,
    )

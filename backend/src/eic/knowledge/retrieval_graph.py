"""请求内RAG子图；可信服务闭包不进入图状态，不配置持久化或重试。"""
from typing import Any
from dataclasses import dataclass
from functools import lru_cache
from collections.abc import Awaitable, Callable

from langchain_core.documents import Document
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from typing_extensions import TypedDict

from eic.core.runtime_refs import RuntimeReferenceUnavailable
from eic.core.observability import observe_retrieval_stage
from eic.knowledge.retrieval import EICRetriever
from eic.vector.qdrant_reader import VectorHit


class RetrievalState(TypedDict, total=False):
    settings: Any
    chunks: list[Any]
    documents: dict[int, Any]
    filtered: list[VectorHit]
    parent_contents: dict[Any, Any]
    dense_documents: list[Document]
    sparse_documents: list[Document]
    dense_hits: list[VectorHit]
    sparse_hits: list[VectorHit]
    sparse_status: str
    dense_status: str
    fusion_hits: list[VectorHit]
    reranked_hits: list[VectorHit] | None
    rerank_status: str
    envelope: dict[str, Any]


def _recall_documents(hits, *, tenant_id, knowledge_base_id):
    return [Document(
        page_content=str(hit.payload.get("content", "")),
        metadata={"tenant_id": tenant_id, "knowledge_base_id": knowledge_base_id,
                  "chunk_id": str(hit.point_id), "score": hit.score,
                  "document_id": hit.payload["documentId"],
                  "source_name": hit.payload.get("source_name") or hit.payload.get("sourceName", ""),
                  "pageNumbers": hit.payload.get("pageNumbers") or [],
                  "parent_chunk_id": hit.payload.get("parentChunkId"),
                  "eic_hit_payload": dict(hit.payload)},
    ) for hit in hits]


def _domain_hits(documents, *, tenant_id, knowledge_base_id):
    hits = []
    for document in documents:
        metadata = document.metadata
        if (type(metadata.get("tenant_id")) is not int or metadata["tenant_id"] != tenant_id
                or type(metadata.get("knowledge_base_id")) is not int
                or metadata["knowledge_base_id"] != knowledge_base_id):
            raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
        hits.append(VectorHit(
            point_id=metadata["chunk_id"], score=metadata["score"],
            payload={**metadata["eic_hit_payload"], "content": document.page_content},
        ))
    return hits


@dataclass(frozen=True)
class RetrievalContext:
    tenant_id: int
    nodes: dict[str, Callable[[RetrievalState], Awaitable[dict[str, Any]]]]


@lru_cache(maxsize=1)
def compiled_retrieval_graph():
    """只缓存无请求数据的拓扑；每次调用的服务放在非持久运行上下文。"""
    builder = StateGraph(RetrievalState, context_schema=RetrievalContext)
    previous = START

    def observed(name):
        async def execute(state: RetrievalState, runtime: Runtime[RetrievalContext]):
            with observe_retrieval_stage(name, tenant_id=runtime.context.tenant_id) as summary:
                result = await runtime.context.nodes[name](state)
                for key, target in (("dense_status", "denseStatus"), ("rerank_status", "rerankStatus")):
                    if key in result:
                        summary[target] = result[key]
                return result
        return execute

    for name in ("authorize", "dense_recall", "sparse_recall", "fusion", "rerank", "citations"):
        builder.add_node(name, observed(name))
        builder.add_edge(previous, name)
        previous = name
    builder.add_edge(previous, END)
    return builder.compile()


async def run_retrieval_graph(
    *, session, tenant_id, base, query, extra, filters, limit, before_external,
):
    from eic.knowledge import ops

    async def authorize(state: RetrievalState):
        return await ops._authorize_retrieval(
            session, tenant_id=tenant_id, base=base, query=query,
            extra=extra, filters=filters, limit=limit, before_external=before_external,
        )

    async def dense(state: RetrievalState):
        status = "not_configured"

        async def search(text, *, tenant_id: int, knowledge_base_id: int, limit: int):
            nonlocal status
            if tenant_id != base.tenant_id or knowledge_base_id != base.id:
                raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
            recalled = await ops._recall_dense(
                state, session=session, tenant_id=tenant_id, base=base, query=text,
                filters=filters, before_external=before_external,
            )
            status = recalled["dense_status"]
            return _recall_documents(recalled["dense_hits"], tenant_id=tenant_id,
                                     knowledge_base_id=knowledge_base_id)

        retriever = EICRetriever(search=search, tenant_id=tenant_id, knowledge_base_id=base.id)
        documents = await retriever.ainvoke(query, config={"callbacks": []})
        return {"dense_documents": documents, "dense_status": status}

    async def sparse(state: RetrievalState):
        status = "skipped"
        async def search(text, *, tenant_id: int, knowledge_base_id: int, limit: int):
            nonlocal status
            if tenant_id != base.tenant_id or knowledge_base_id != base.id:
                raise RuntimeReferenceUnavailable("runtime_reference_unavailable")
            recalled = await ops._recall_sparse(state, query=text, session=session,
                tenant_id=tenant_id, base=base, filters=filters)
            status = recalled.get("sparse_status", "unknown")
            return _recall_documents(recalled["sparse_hits"], tenant_id=tenant_id,
                                     knowledge_base_id=knowledge_base_id)

        retriever = EICRetriever(search=search, tenant_id=tenant_id, knowledge_base_id=base.id)
        return {"sparse_documents": await retriever.ainvoke(query, config={"callbacks": []}), "sparse_status": status}

    async def fuse(state: RetrievalState):
        recalled = {
            "dense_hits": _domain_hits(state["dense_documents"], tenant_id=tenant_id, knowledge_base_id=base.id),
            "sparse_hits": _domain_hits(state["sparse_documents"], tenant_id=tenant_id, knowledge_base_id=base.id),
        }
        return {**recalled, **await ops._fuse_retrieval({**state, **recalled})}

    async def rerank(state: RetrievalState):
        return await ops._rerank_retrieval(
            state, session=session, tenant_id=tenant_id, base=base, query=query,
            before_external=before_external,
        )

    async def citations(state: RetrievalState):
        return {"envelope": await ops._finalize_retrieval(state, base=base, query=query, filters=filters,
            session=session, tenant_id=tenant_id)}

    nodes = dict((
        ("authorize", authorize), ("dense_recall", dense), ("sparse_recall", sparse),
        ("fusion", fuse), ("rerank", rerank), ("citations", citations),
    ))
    result = await compiled_retrieval_graph().ainvoke({}, config={"callbacks": []},
        context=RetrievalContext(tenant_id, nodes))
    return result["envelope"]

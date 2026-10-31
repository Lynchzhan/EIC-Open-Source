"""关系库倒排索引；正文仅在索引写入和最终候选回填时读取。"""
from collections import Counter
import hashlib
import math

from sqlalchemy import case, delete, func, insert, select

from eic.knowledge.retrieval import Bm25Index, filter_by_metadata, tokenize
from eic.models import KnowledgeChunk, KnowledgeDocument, KnowledgeSparseChunk, KnowledgeSparseTerm
from eic.vector.qdrant_reader import VectorHit


def tokenizer_version():
    try:
        import jieba
        return "jieba:" + jieba.__version__ + ":lower:v1"
    except ImportError:
        return "cjk-regex:lower:v1"


def term_key(term):
    return hashlib.sha256(term.encode("utf-8")).hexdigest()


async def insert_new_chunks(session, chunks):
    """新分配的 chunk ID 不需要先删索引；词项使用 executemany 有界提交。"""
    version = tokenizer_version()
    headers, terms = [], []
    for chunk in chunks:
        counts = Counter(tokenize(chunk['content']))
        headers.append(dict(chunk_id=chunk['id'], content_hash=chunk['content_hash'],
                            tokenizer_version=version, token_count=sum(counts.values())))
        terms.extend(dict(chunk_id=chunk['id'], term_hash=term_key(term), frequency=count)
                     for term, count in counts.items())
    if headers:
        await session.execute(insert(KnowledgeSparseChunk), headers)
    for offset in range(0, len(terms), 500):
        await session.execute(insert(KnowledgeSparseTerm), terms[offset:offset + 500])


async def index_chunk(session, chunk):
    """与 child chunk 共用调用者事务；重建相同 chunk 不累计旧词项。"""
    await session.execute(delete(KnowledgeSparseTerm).where(KnowledgeSparseTerm.chunk_id == chunk.id))
    await session.execute(delete(KnowledgeSparseChunk).where(KnowledgeSparseChunk.chunk_id == chunk.id))
    counts = Counter(tokenize(chunk.content))
    session.add(KnowledgeSparseChunk(chunk_id=chunk.id, content_hash=chunk.content_hash,
        tokenizer_version=tokenizer_version(), token_count=sum(counts.values())))
    await session.flush()
    session.add_all(KnowledgeSparseTerm(chunk_id=chunk.id, term_hash=term_key(term), frequency=count)
                    for term, count in counts.items())


def scope_query(tenant_id, base_id, document_ids):
    return select(KnowledgeChunk.id).join(KnowledgeDocument,
        KnowledgeDocument.id == KnowledgeChunk.document_id).where(
        KnowledgeChunk.tenant_id == tenant_id, KnowledgeChunk.knowledge_base_id == base_id,
        KnowledgeDocument.tenant_id == tenant_id, KnowledgeDocument.knowledge_base_id == base_id,
        KnowledgeDocument.id.in_(document_ids), KnowledgeDocument.is_deleted.is_(False),
        KnowledgeDocument.active_source_revision == KnowledgeDocument.source_revision,
        KnowledgeChunk.index_version == KnowledgeDocument.active_index_version,
        KnowledgeChunk.chunk_index >= 0,
        func.coalesce(KnowledgeChunk.attributes["chunk_kind"].as_string(), "child") == "child")


async def metadata_scope(session, statement, filters):
    if not filters:
        return statement
    # 任意嵌套 JSON 保留原 Python 等值语义；不把全文载入或改成不等价的 JSON 比较。
    if "content" in filters:
        value = filters["content"]
        if not isinstance(value, str):
            return statement.where(KnowledgeChunk.id < 0)
        statement = statement.where(KnowledgeChunk.content == value)
        filters = {key: value for key, value in filters.items() if key != "content"}
    rows = await session.stream(select(KnowledgeChunk.id, KnowledgeChunk.attributes,
        KnowledgeChunk.document_id, KnowledgeChunk.parent_chunk_id).where(KnowledgeChunk.id.in_(statement)))
    allowed = []
    async for identity, metadata, document_id, parent_id in rows:
        payload = {**(metadata or {}), "documentId": document_id, "parentChunkId": parent_id}
        if filter_by_metadata([VectorHit(str(identity), 0., payload)], filters):
            allowed.append(identity)
    # 使用子查询保留实际授权及版本条件。
    return statement.where(KnowledgeChunk.id.in_(allowed))


async def search_index(session, statement, query, *, limit):
    terms = Counter(term_key(term) for term in tokenize(query))
    if not terms:
        return [], "ok"
    if len(terms) > 512:
        return [], "query_too_large"
    stats = KnowledgeSparseChunk
    valid = select(stats.chunk_id).join(KnowledgeChunk, KnowledgeChunk.id == stats.chunk_id).where(
        stats.chunk_id.in_(statement), stats.tokenizer_version == tokenizer_version(),
        stats.content_hash == KnowledgeChunk.content_hash)
    missing = await session.scalar(select(KnowledgeChunk.id).where(
        KnowledgeChunk.id.in_(statement), KnowledgeChunk.id.not_in(valid)).limit(1))
    if missing is not None:
        # 小型历史库兼容；大库要求显式离线 backfill，查询不执行无界重建。
        identities = list(await session.scalars(select(KnowledgeChunk.id).where(
            KnowledgeChunk.id.in_(statement)).order_by(KnowledgeChunk.chunk_index, KnowledgeChunk.id).limit(257)))
        if len(identities) > 256:
            return [], "backfill_required"
        chunks = list(await session.scalars(select(KnowledgeChunk).where(
            KnowledgeChunk.id.in_(identities)).order_by(KnowledgeChunk.chunk_index, KnowledgeChunk.id)))
        candidates = [VectorHit(str(c.id), 0., {**(c.attributes or {}), "content": c.content,
            "documentId": c.document_id, "parentChunkId": c.parent_chunk_id}) for c in chunks]
        return Bm25Index(candidates).search(query, limit=limit), "legacy_bounded"
    # MySQL 对整数 AVG 使用有限小数；乘浮点数后与参考 BM25 共用双精度语义。
    count, average = (await session.execute(select(func.count(), func.avg(stats.token_count * 1.0)).where(
        stats.chunk_id.in_(valid)))).one()
    if not count:
        return [], "ok"
    postings = KnowledgeSparseTerm
    frequencies = dict((await session.execute(select(postings.term_hash, func.count()).where(
        postings.chunk_id.in_(valid), postings.term_hash.in_(terms)).group_by(postings.term_hash))).all())
    if not frequencies:
        return [], "ok"
    weights = {term: repeat * math.log(1 + (count - frequencies[term] + .5) / (frequencies[term] + .5))
               for term, repeat in terms.items() if term in frequencies}
    weight = case(weights, value=postings.term_hash, else_=0.)
    score = func.sum(weight * postings.frequency * 2.5 /
        (postings.frequency + 1.5 * (.25 + .75 * stats.token_count / float(average or 1.))))
    rows = (await session.execute(select(postings.chunk_id, score.label("score")).join(stats,
        stats.chunk_id == postings.chunk_id).join(KnowledgeChunk, KnowledgeChunk.id == postings.chunk_id).where(
        postings.chunk_id.in_(valid), postings.term_hash.in_(weights)).group_by(
        postings.chunk_id, KnowledgeChunk.chunk_index).order_by(score.desc(),
        KnowledgeChunk.chunk_index, postings.chunk_id).limit(limit))).all()
    return [VectorHit(str(identity), float(value), {}) for identity, value in rows], "ok"

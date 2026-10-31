"""跨知识库只融合已授权结果的排名，保留原始分数用于诊断。"""
import hashlib


def fuse_ranked_results(batches, *, limit):
    """RRF 不比较不同向量、BM25 或重排模型的分数；同分保持输入顺序。"""
    ranked = {}
    for base_id, rows in batches:
        seen = set()
        for rank, row in enumerate(rows, 1):
            chunk_id = row.get("chunkId")
            identity = (str(base_id), str(chunk_id)) if chunk_id else (
                str(base_id), str(row.get("documentId")),
                hashlib.sha256(str(row.get("content") or "").encode("utf-8")).hexdigest(),
            )
            if identity in seen:
                continue
            seen.add(identity)
            score = 1.0 / (60 + rank)
            if identity not in ranked:
                ranked[identity] = {
                    **row, "retrievalScore": row.get("score"),
                    "retrievalScoreSpace": row.get("scoreSpace"),
                    "score": score, "scoreSpace": "cross_base_rrf",
                    "crossBaseRank": rank,
                }
            else:
                ranked[identity]["score"] += score
    return sorted(ranked.values(), key=lambda item: item["score"], reverse=True)[:limit]

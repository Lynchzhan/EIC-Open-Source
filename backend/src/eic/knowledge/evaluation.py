from __future__ import annotations

from typing import Any, Iterable
import math


def ndcg_at_k(ranked_ids: list[str], relevance: dict[str, float], k: int) -> float:
    """相关性 gain 为 2**grade-1；重复结果占排名但不重复获得增益。"""
    if type(k) is not int or k < 1:
        return 0.0
    if any(type(value) not in (int, float) or not math.isfinite(value)
           or not 0 <= value <= 10 for value in relevance.values()):
        raise ValueError("相关性等级必须为 0 到 10 之间的有限数值")
    seen, dcg = set(), 0.0
    for rank, item in enumerate(ranked_ids[:k], 1):
        if item not in seen:
            dcg += (2 ** relevance.get(item, 0) - 1) / math.log2(rank + 1)
        seen.add(item)
    ideal = sum((2 ** value - 1) / math.log2(rank + 1)
                for rank, value in enumerate(sorted(relevance.values(), reverse=True)[:k], 1))
    return dcg / ideal if ideal else 0.0


def _as_id_set(values: Iterable[Any] | None) -> set[str]:
    ids: set[str] = set()
    for value in values or []:
        if value in (None, "", 0, "0"):
            continue
        ids.add(str(value))
    return ids


def hit_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if k < 1:
        return 0.0
    return 1.0 if relevant_ids.intersection(ranked_ids[:k]) else 0.0


def recall_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    if not relevant_ids or k < 1:
        return 0.0
    return len(relevant_ids.intersection(ranked_ids[:k])) / len(relevant_ids)


def mean_reciprocal_rank(ranked_ids: list[str], relevant_ids: set[str]) -> float:
    for index, item_id in enumerate(ranked_ids, start=1):
        if item_id in relevant_ids:
            return 1.0 / index
    return 0.0


def result_match_ids(result: dict[str, Any]) -> set[str]:
    ids = _as_id_set([
        result.get("documentId"),
        result.get("chunkId"),
        (result.get("metadata") or {}).get("documentId"),
        (result.get("metadata") or {}).get("source_name"),
        result.get("documentName"),
    ])
    return ids


def evaluate_case(result_rows: list[dict[str, Any]], expected_document_ids: Iterable[Any] | None, expected_chunk_ids: Iterable[Any] | None, *, k: int) -> dict[str, Any]:
    expected_docs = _as_id_set(expected_document_ids)
    expected_chunks = _as_id_set(expected_chunk_ids)
    relevant = {("document", item) for item in expected_docs} | {("chunk", item) for item in expected_chunks}
    ranked_document_ids: list[str] = []
    ranked_chunk_ids: list[str] = []
    seen_docs: set[str] = set()
    for row in result_rows:
        chunk_id = str(row.get("chunkId") or "")
        document_id = str(row.get("documentId") or "")
        if chunk_id:
            ranked_chunk_ids.append(chunk_id)
        if document_id and document_id not in seen_docs:
            ranked_document_ids.append(document_id)
            seen_docs.add(document_id)
    matched = []
    covered = set()
    reciprocal = 0.0
    for rank, row in enumerate(result_rows[:max(k, 0)], start=1):
        ids = {("document", str(row.get("documentId") or "")), ("chunk", str(row.get("chunkId") or ""))}
        found = ids & relevant
        if found:
            matched.append(row)
            covered.update(found)
            if reciprocal == 0:
                reciprocal = 1.0 / rank
    return {
        "k": k,
        "hitAtK": 1.0 if matched else 0.0,
        "recallAtK": len(covered) / len(relevant) if relevant else 0.0,
        "mrr": reciprocal,
        "ndcgAtK": max(
            ndcg_at_k(ranked_document_ids, dict.fromkeys(expected_docs, 1), k),
            ndcg_at_k(ranked_chunk_ids, dict.fromkeys(expected_chunks, 1), k),
        ),
        "relevantCount": len(relevant),
        "matchedCount": len(matched),
        "hit": bool(matched),
        "rankedDocumentIds": ranked_document_ids[:k],
        "rankedChunkIds": ranked_chunk_ids[:k],
        "matched": matched,
    }


def aggregate_eval_scores(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not case_results:
        return {"caseCount": 0, "hitAtK": 0.0, "recallAtK": 0.0, "mrr": 0.0, "misses": []}
    count = len(case_results)
    misses = [item for item in case_results if not item.get("hit")]
    return {
        "caseCount": count,
        "hitAtK": sum(float(item.get("hitAtK") or 0) for item in case_results) / count,
        "recallAtK": sum(float(item.get("recallAtK") or 0) for item in case_results) / count,
        "mrr": sum(float(item.get("mrr") or 0) for item in case_results) / count,
        "ndcgAtK": sum(float(item.get("ndcgAtK") or 0) for item in case_results) / count,
        "missCount": len(misses),
        "misses": misses,
    }

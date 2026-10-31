import pytest

from eic.knowledge.evaluation import evaluate_case
from eic.knowledge.retrieval import rrf_fuse
from eic.knowledge.strategy import RetrievalSettings, build_result_payload, fuse_strategy_hits, fusion_candidates, merge_retrieval_config
from eic.vector.qdrant_reader import VectorHit


def hit(identifier, score):
    return VectorHit(identifier, score, {"content": identifier, "documentId": identifier})


def test_rrf_uses_rank_score_not_last_channel_value():
    dense = [hit("same", .99), hit("dense", .8)]
    sparse = [hit("sparse", 22.), hit("same", 19.)]
    fused = rrf_fuse([dense, sparse], k=60)
    assert fused[0].point_id == "same"
    assert fused[0].score == pytest.approx(1/61 + 1/62)
    assert fused[0].payload["fusionScore"] == fused[0].score
    assert fused[0].score != sparse[1].score


def test_candidate_rerank_and_final_budgets_and_independent_thresholds():
    settings = RetrievalSettings("full", merge_retrieval_config({
        "finalLimit": 2, "candidateLimit": 12, "rerankTopN": 8,
        "thresholdVersion": 2, "denseThreshold": .3, "sparseThreshold": 5.,
        "fusionThreshold": .01, "rerankThreshold": .7,
    }))
    dense = [hit(str(i), .9) for i in range(12)]
    sparse = [hit(str(i), 20.) for i in range(12)]
    candidates = fusion_candidates(settings, dense, sparse)
    assert len(candidates) == 12
    # 原融合排名最后的候选经 rerank 升至第一，不被 finalLimit 提前裁掉。
    reranked = [VectorHit(candidates[-1].point_id, .95, {**candidates[-1].payload, "rerankScore": .95}),
                VectorHit(candidates[0].point_id, .1, {**candidates[0].payload, "rerankScore": .1})]
    final = fuse_strategy_hits("q", settings=settings, dense_hits=dense, sparse_hits=sparse, reranked_hits=reranked)
    assert [h.point_id for h in final] == [candidates[-1].point_id]
    rows = build_result_payload(final, parent_contents={}, documents={}, settings=settings,
                                dense_scores={str(i): .9 for i in range(12)}, sparse_scores={str(i): 20. for i in range(12)})
    assert rows[0]["vectorScore"] == .9 and rows[0]["bm25Score"] == 20.
    assert rows[0]["fusionScore"] != rows[0]["rerankScore"] and rows[0]["scoreSpace"] == "rerank"


@pytest.mark.parametrize("field", ["scoreThreshold", "score_threshold"])
def test_legacy_threshold_not_reinterpreted_in_fusion_space(field):
    config = merge_retrieval_config({field: .7})
    assert config["thresholdVersion"] == 1 and config["thresholdStatus"] == "legacy_requires_review"
    assert not fuse_strategy_hits("q", settings=RetrievalSettings("advanced", config),
                                  dense_hits=[hit("a", .9)], sparse_hits=[hit("a", 3.)])
    assert fuse_strategy_hits("q", settings=RetrievalSettings("basic", config),
                              dense_hits=[hit("a", .9)], sparse_hits=[])


@pytest.mark.parametrize("value", [float("inf"), float("nan"), "bad", None])
def test_nonfinite_config_scores_normalize(value):
    config = merge_retrieval_config({"thresholdVersion": 2, "denseThreshold": value})
    assert config["denseThreshold"] == 0.


@pytest.mark.parametrize("expected_docs,expected_chunks", [([102], []), ([], [202]), ([102], [202])])
def test_evaluation_rank_is_result_position_not_identifier_count(expected_docs, expected_chunks):
    rows = [{"documentId": 101, "chunkId": 201, "documentName": "first"},
            {"documentId": 102, "chunkId": 202, "documentName": "second"}]
    score = evaluate_case(rows, expected_docs, expected_chunks, k=2)
    assert score["hit"] and score["hitAtK"] == 1. and score["recallAtK"] == 1. and score["mrr"] == .5
    missed = evaluate_case(rows, expected_docs, expected_chunks, k=1)
    assert not missed["hit"] and missed["mrr"] == 0.


def test_evaluation_identifier_spaces_and_names_do_not_alias():
    rows = [{"documentId": 42, "chunkId": 99, "documentName": "99"}]
    assert not evaluate_case(rows, [99], [], k=1)["hit"]
    assert not evaluate_case(rows, [], [42], k=1)["hit"]
    assert evaluate_case(rows, [42], [99], k=1)["relevantCount"] == 2


def test_rrf_duplicate_within_one_channel_does_not_inflate_score():
    fused = rrf_fuse([[hit("a", .9), hit("a", .9)], [hit("a", 9.)]])
    assert len(fused) == 1 and fused[0].score == pytest.approx(2 / 61)

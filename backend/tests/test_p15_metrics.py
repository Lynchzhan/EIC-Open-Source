import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts"))
from harness.boundaries import HarnessError
from harness.metrics import agent_metrics, citation_metrics, ranking_metrics
from eic.knowledge.evaluation import ndcg_at_k


def test_ranking_known_good_bad_and_duplicate_have_exact_scores():
    good = ranking_metrics(["a", "b"], {"a": 3, "b": 1}, 2)
    assert good["ndcgAtK"] == good["hitAtK"] == good["recallAtK"] == good["mrr"] == 1
    bad = ranking_metrics(["x", "b", "a"], {"a": 3, "b": 1}, 2)
    assert bad["recallAtK"] == .5 and bad["mrr"] == .5 and 0 < bad["ndcgAtK"] < .1
    duplicate = ranking_metrics(["a", "a"], {"a": 3, "b": 1}, 2)
    assert duplicate["recallAtK"] == .5 and duplicate["ndcgAtK"] < 1
    assert duplicate["ndcgAtK"] == ndcg_at_k(["a", "a"], {"a": 3, "b": 1}, 2)
    assert ranking_metrics([], {}, 2)["ndcgAtK"] is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1, 11, True, "3"])
def test_nonfinite_and_invalid_relevance_rejected(bad):
    with pytest.raises(HarnessError):
        ranking_metrics(["a"], {"a": bad}, 1)
    with pytest.raises(ValueError):
        ndcg_at_k(["a"], {"a": bad}, 1)


def test_missing_and_unknown_citations_never_become_success():
    gold = {"claim1": ["s1"], "claim2": ["s2"]}
    observed = [{"claimId": "claim1", "sourceId": "s1"}, {"claimId": "claim2", "sourceId": "unknown"}]
    result = citation_metrics(observed, gold, ["s1", "s2"])
    assert result["citationPrecision"] == result["citationRecall"] == .5
    assert citation_metrics([], gold, [])["citationPrecision"] == 0
    assert citation_metrics([], {}, [])["citationRecall"] is None


def test_agent_metrics_keep_na_denominators_and_measurements_separate():
    result = agent_metrics([
        {"taskSuccessRate": True, "crashRecoveryRate": None, "latencyMs": 10, "firstTokenMs": 2,
         "inputEstimate": 100, "retryCount": 1},
        {"taskSuccessRate": False, "crashRecoveryRate": True, "latencyMs": 30, "firstTokenMs": None,
         "inputEstimate": 200, "retryCount": 0},
    ])
    assert result["metrics"]["taskSuccessRate"] == .5
    assert result["metrics"]["crashRecoveryRate"] == 1
    assert result["sampleCounts"]["crashRecoveryRate"] == 1
    assert result["metrics"]["p50LatencyMs"] == 20
    assert math.isclose(result["metrics"]["p95LatencyMs"], 29)
    assert result["metrics"]["inputEstimate"] == 300
    assert result["metrics"]["inputTokens"] is None and result["metrics"]["providerCost"] is None
    with pytest.raises(HarnessError):
        agent_metrics([{"taskSuccessRate": "true"}])
    with pytest.raises(HarnessError):
        agent_metrics([{"latencyMs": float("nan")}])

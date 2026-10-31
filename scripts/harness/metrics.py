"""有明确分母的确定性指标；缺失测量返回 None，不当作 0 或成功。"""
import math

from .boundaries import HarnessError


def number(value, name, *, maximum=None):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise HarnessError("指标数值无效：" + name)
    if maximum is not None and value > maximum:
        raise HarnessError("指标超出范围：" + name)
    return float(value)


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128:
        raise HarnessError("指标 ID 无效")
    return value


def ranking_metrics(ranked, relevance, k):
    if type(k) is not int or not 0 < k <= 1000:
        raise HarnessError("K 必须为正整数")
    if not isinstance(ranked, list) or len(ranked) > 10000 or not isinstance(relevance, dict):
        raise HarnessError("排序指标输入结构无效")
    for item in ranked:
        identifier(item)
    gold = {identifier(key): number(value, "relevance", maximum=10) for key, value in relevance.items()}
    positives = {key for key, value in gold.items() if value > 0}
    empty = {"hitAtK": None, "recallAtK": None, "mrr": None, "ndcgAtK": None,
             "relevantCount": 0, "matchedCount": 0}
    if not positives:
        return empty
    top = ranked[:k]
    matched = positives.intersection(top)
    seen, dcg = set(), 0.0
    for rank, key in enumerate(top, 1):
        # 重复项仍占排名位置，但第二次不产生相关性增益。
        if key not in seen:
            dcg += (2 ** gold.get(key, 0) - 1) / math.log2(rank + 1)
        seen.add(key)
    ideal = sum((2 ** gain - 1) / math.log2(rank + 1)
                for rank, gain in enumerate(sorted(gold.values(), reverse=True)[:k], 1))
    reciprocal = next((1 / rank for rank, key in enumerate(ranked, 1) if key in positives), 0.0)
    return {"hitAtK": float(bool(matched)), "recallAtK": len(matched) / len(positives),
            "mrr": reciprocal, "ndcgAtK": dcg / ideal if ideal else None,
            "relevantCount": len(positives), "matchedCount": len(matched)}


def citation_metrics(items, gold_claims, available_sources):
    if not isinstance(items, list) or not isinstance(gold_claims, dict) or not isinstance(available_sources, list):
        raise HarnessError("引用指标结构无效")
    available = {identifier(value) for value in available_sources}
    gold = {}
    for claim, sources in gold_claims.items():
        if not isinstance(sources, list) or not sources:
            raise HarnessError("引用 gold 每个 claim 必须有证据")
        gold[identifier(claim)] = {identifier(value) for value in sources}
    covered, valid = set(), 0
    for item in items:
        if not isinstance(item, dict) or set(item) != {"claimId", "sourceId"}:
            raise HarnessError("引用项结构无效")
        claim, source = identifier(item["claimId"]), identifier(item["sourceId"])
        if source in available and source in gold.get(claim, set()):
            valid += 1
            covered.add(claim)
    return {"citationPrecision": valid / len(items) if items else (0.0 if gold else None),
            "citationRecall": len(covered) / len(gold) if gold else None,
            "citationCount": len(items), "coveredClaims": len(covered)}


def fraction(numerator, denominator):
    return numerator / denominator if denominator else None


def percentile(values, p):
    checked = sorted(number(item, "latency") for item in values)
    if not checked:
        return None
    offset = (len(checked) - 1) * p
    lower = math.floor(offset)
    upper = math.ceil(offset)
    return checked[lower] + (checked[upper] - checked[lower]) * (offset - lower)


def average(values):
    measured = [number(item, "metric", maximum=1) for item in values if item is not None]
    return sum(measured) / len(measured) if measured else None


def agent_metrics(observations):
    """调用方必须提交实测值或 None；不推断缺失上游账单和首 token。"""
    counts = {name: [0, 0] for name in [
        "taskSuccessRate", "toolSelectionAccuracy", "toolArgumentValidity", "toolSuccessRate",
        "crashRecoveryRate", "duplicateSideEffectRate", "fallbackSuccessRate",
        "uncertainOutcomeRate", "maxStepViolationRate", "answerCorrectness",
        "answerCompleteness", "refusalAccuracy",
    ]}
    latencies, first_tokens, retries = [], [], []
    usage = {"inputTokens": [], "outputTokens": [], "providerCost": [], "inputEstimate": [], "outputEstimate": []}
    for row in observations:
        if not isinstance(row, dict):
            raise HarnessError("Agent 观测结构无效")
        for name in counts:
            value = row.get(name)
            if value is not None:
                if type(value) is not bool:
                    raise HarnessError("Agent 比率必须使用布尔观测：" + name)
                counts[name][0] += int(value)
                counts[name][1] += 1
        for key, target in [("latencyMs", latencies), ("firstTokenMs", first_tokens), ("retryCount", retries)]:
            if row.get(key) is not None:
                target.append(number(row[key], key))
        for key in usage:
            if row.get(key) is not None:
                usage[key].append(number(row[key], key))
    metrics = {key: fraction(*values) for key, values in counts.items()}
    metrics.update({
        "p50LatencyMs": percentile(latencies, .5), "p95LatencyMs": percentile(latencies, .95),
        "p50FirstTokenMs": percentile(first_tokens, .5), "p95FirstTokenMs": percentile(first_tokens, .95),
        "retryCount": sum(retries) if retries else None,
        **{key: sum(values) if values else None for key, values in usage.items()},
    })
    return {"metrics": metrics, "sampleCounts": {
        **{key: value[1] for key, value in counts.items()},
        "latencyMs": len(latencies), "firstTokenMs": len(first_tokens),
        **{key: len(values) for key, values in usage.items()},
    }, "providerCostUnit": "upstream_reported_currency_not_reserved_units"}

"""显式思考能力契约；不根据模型名称猜测上游支持。"""
import re

from eic.ai.budget import ChatConfigurationError


EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
DISABLED = {"enabled": False, "adapter": "openai", "levels": [], "default": None, "budgets": {}}


def normalize_policy(value, provider, output_limit=8192):
    if value is None:
        return {**DISABLED}
    if not isinstance(value, dict) or type(value.get("enabled", False)) is not bool:
        raise ValueError("思考配置格式无效")
    if not value.get("enabled"):
        return {**DISABLED}
    adapter = value.get("adapter", "openai")
    provider = provider.lower().strip()
    expected = "anthropic" if provider == "anthropic" else "ollama" if provider in {"ollama", "10"} else "openai"
    if adapter != expected:
        raise ValueError("思考参数映射与请求协议不匹配")
    levels = value.get("levels")
    allowed = {"off", "on"} if adapter == "ollama" else {"off", "low", "medium", "high"} if adapter == "anthropic" else EFFORTS
    if (not isinstance(levels, list) or not levels or len(levels) > len(allowed)
            or any(not isinstance(level, str) or level not in allowed for level in levels)
            or len(set(levels)) != len(levels) or value.get("default") not in levels):
        raise ValueError("请选择上游实际支持的思考档位及其中一个默认档位")
    budgets = {}
    if adapter == "anthropic":
        supplied = value.get("budgets") or {}
        if not isinstance(supplied, dict):
            raise ValueError("思考 Token 预算格式无效")
        for level in levels:
            if level == "off":
                continue
            budget = supplied.get(level)
            if type(budget) is not int or not 1024 <= budget < output_limit:
                raise ValueError("Messages 思考预算至少 1024，且必须小于回答 Token 上限")
            budgets[level] = budget
    return {"enabled": True, "adapter": adapter, "levels": levels[:], "default": value["default"], "budgets": budgets}


def effective_reasoning(model, selection=None, *, output_limit=None):
    try:
        policy = normalize_policy(getattr(model, "reasoning_policy", None), model.provider,
                                  model.max_output_tokens or 8192)
    except ValueError:
        raise ChatConfigurationError("invalid_reasoning_policy") from None
    selected = None
    changed = False
    if selection is not None:
        if (not isinstance(selection, dict) or set(selection) != {"modelId", "effort"}
                or not isinstance(selection.get("modelId"), str)
                or re.fullmatch(r"[0-9]{1,19}", selection["modelId"]) is None
                or not 0 < int(selection["modelId"]) < 2**63):
            raise ChatConfigurationError("invalid_reasoning_selection")
        selected = selection.get("effort")
        if not isinstance(selected, str) or len(selected) > 16:
            raise ChatConfigurationError("invalid_reasoning_selection")
        changed = (str(selection.get("modelId")) != str(model.id) or selected not in policy["levels"])
    effort = (selected if selected and not changed else policy["default"]) if policy["enabled"] else None
    metadata = {"enabled": policy["enabled"], "effort": effort, "modelId": str(model.id),
                "source": "session" if selected and not changed else "administrator_default",
                "adjusted": bool(selected and (changed or not policy["enabled"]))}
    parameters = {}
    if not policy["enabled"]:
        return parameters, metadata
    if policy["adapter"] == "openai":
        parameters["reasoning_effort"] = effort
    elif policy["adapter"] == "ollama":
        parameters["think"] = effort == "on"
    elif effort == "off":
        parameters["thinking"] = {"type": "disabled"}
    else:
        budget = policy["budgets"][effort]
        if output_limit is not None and budget >= output_limit:
            raise ChatConfigurationError("reasoning_budget_exceeds_output")
        parameters["thinking"] = {"type": "enabled", "budget_tokens": budget}
        metadata["budgetTokens"] = budget
    return parameters, metadata


def capabilities(model, selection=None):
    policy = normalize_policy(getattr(model, "reasoning_policy", None), model.provider, model.max_output_tokens or 8192)
    _, effective = effective_reasoning(model, selection)
    return {"modelId": str(model.id), "enabled": policy["enabled"], "levels": policy["levels"],
            "default": policy["default"], "selection": selection if not effective["adjusted"] else None,
            "effective": effective, "upstreamAcceptanceVerified": False}

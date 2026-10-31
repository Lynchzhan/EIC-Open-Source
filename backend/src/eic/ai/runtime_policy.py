"""会话硬预算不能被重试、换模型、切智能体或进程重启重置。"""
from contextvars import ContextVar
from copy import deepcopy
import asyncio
import time
import uuid
from datetime import UTC

from sqlalchemy import select, update

from eic.core.database import SessionLocal
from eic.models import ChatMessage, ChatSession, ConversationBudget, GenerationLease, ObservabilityEvent
from eic.ai.budget import text_units


BUDGET_HINTS = {
    "conversation_budget_exceeded": "会话预算校验未通过，请展开预算详情查看具体原因。",
    "conversation_token_limit": "会话剩余 Token 不足以预留本次调用，请查看预算详情或联系管理员调整会话预算。",
    "conversation_call_limit": "会话累计调用次数已达上限，请查看预算详情或联系管理员调整策略。",
    "conversation_time_limit": "会话累计执行时长已耗尽，请查看预算详情。",
    "conversation_usage_incomplete": "历史 Token 用量尚未核实，系统未按零计算；请联系管理员核对记录，或新建会话继续。",
    "conversation_tiers_exhausted": "模型降级档位已耗尽，请检查各档位用量和失败记录。",
    "conversation_request_pending": "会话已有执行中或待核对的请求，请先刷新状态并等待原请求结束。",
}

ACTIVE = ContextVar("conversation_runtime_budget", default=None)
DEFAULTS = {"maxCalls": 100, "maxTokens": 1000000, "maxSeconds": 1800,
            "maxOutputTokens": 4096, "callTimeoutSeconds": 120, "maxSteps": 8}
CEILINGS = {"maxCalls": 1000, "maxTokens": 10000000, "maxSeconds": 86400,
            "maxOutputTokens": 32768, "callTimeoutSeconds": 600, "maxSteps": 8}


class BudgetStopped(Exception):
    # 不继承 ProviderError，检索的 Provider 降级不能吞掉硬预算拒绝。
    def __init__(self, message, *, code="conversation_budget_exceeded"):
        super().__init__(message)
        self.reason = code


def execution_timeout_ms(policy, provider_timeout_seconds):
    """显式策略优先；未配置策略时沿用部署的模型超时，不隐含截成30秒。"""
    return policy["maxSeconds"] * 1000 if policy else provider_timeout_seconds * 1000


def normalize_policy(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("运行限制必须为对象")
    result = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if type(value) is not int or not 1 <= value <= CEILINGS[key]:
            raise ValueError(f"{key} 必须为 1 到 {CEILINGS[key]} 的整数")
        result[key] = value
    result["action"] = raw.get("action", "stop")
    if result["action"] not in {"stop", "degrade"}:
        raise ValueError("预算处理方式无效")
    tiers = raw.get("tiers", [])
    if not isinstance(tiers, list) or len(tiers) > 10:
        raise ValueError("降级档位最多 10 个")
    result["tiers"] = []
    if result["action"] == "degrade" and not tiers:
        raise ValueError("自动降级至少需要配置一个模型档位")
    if result["action"] == "stop":
        # 停止模式保留草稿，只有启用降级时才校验和使用这些字段。
        result["tiers"] = [
            {key: deepcopy(tier[key]) for key in ("modelId", "maxCalls", "maxTokens") if key in tier}
            for tier in tiers if isinstance(tier, dict)
        ]
        return result
    seen = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            raise ValueError("降级档位无效")
        model_id = tier.get("modelId")
        if isinstance(model_id, bool) or not str(model_id).isdigit() or not 0 < int(model_id) < 2**63:
            raise ValueError("降级模型 ID 无效")
        if str(model_id) in seen:
            raise ValueError("降级模型不能重复")
        seen.add(str(model_id))
        item = {"modelId": str(model_id)}
        for field in ("maxCalls", "maxTokens"):
            value = tier.get(field, result[field])
            if type(value) is not int or not 1 <= value <= result[field]:
                raise ValueError("档位阈值必须为正数且不能超过会话硬限制")
            item[field] = value
        result["tiers"].append(item)
    return result


def raise_token_budget(state, minimum, *, app_id):
    """仅显式管理迁移调用；不允许切换助手自动放宽已有会话或重置计数。"""
    if type(minimum) is not int or not 1 <= minimum <= CEILINGS["maxTokens"]:
        raise ValueError("invalid_token_limit")
    result = deepcopy(state)
    old = result["policy"]["maxTokens"]
    if old < minimum:
        result["policy"]["maxTokens"] = minimum
        result.setdefault("policyChanges", []).append({"field": "maxTokens", "before": old,
            "after": minimum, "appId": str(app_id), "source": "administrator_approved_migration"})
    return result


async def close_abandoned_turn(session, message, *, ended_at):
    """由已核验租约的恢复者在同一事务收尾；未知预留继续计费。"""
    await session.execute(update(ChatSession).where(
        ChatSession.id == message.session_id, ChatSession.tenant_id == message.tenant_id,
    ).values(updated_at=ChatSession.updated_at))
    row = await session.get(ConversationBudget, message.session_id, populate_existing=True)
    if row is None or row.tenant_id != message.tenant_id:
        return
    state = deepcopy(row.state)
    turn = state.get("turns", {}).get(message.request_id)
    if not turn or turn.get("status") != "pending":
        return
    end = ended_at.replace(tzinfo=UTC).timestamp()
    duration = min(max(0, end - turn["started"]),
                   turn.get("limitSeconds", state["policy"]["maxSeconds"]))
    turn.update(seconds=turn.get("seconds", 0) + duration, status="interrupted",
                recoveryReason="producer_lease_closed", providerOutcome="uncertain")
    for entry in state.get("entries", {}).values():
        if entry.get("status") == "pending" and entry.get("requestId", message.request_id) == message.request_id:
            entry["status"] = "unknown"
    row.state = state


def compact_state(state, keep=128):
    """只归并已结算项；未知预留和待处理轮次始终保留。"""
    history = state["history"]
    entries = state.get("entries", {})
    settled = [key for key, entry in entries.items() if entry.get("status") == "settled"]
    for key in settled[:-keep]:
        entry = entries.pop(key)
        history["calls"] += 1
        history["tokens"] += entry["tokens"]
        if entry["kind"] == "model":
            tier = history.setdefault("tiers", {}).setdefault(str(entry["tier"]), {"calls": 0, "tokens": 0})
            tier["calls"] += 1
            tier["tokens"] += entry["tokens"]
    turns = state.get("turns", {})
    closed = [key for key, turn in turns.items() if turn.get("status") != "pending"]
    for key in closed[:-keep]:
        history["seconds"] += turns.pop(key).get("seconds", 0)


def settle_call_state(state, key, sink, elapsed, failed, *, reason):
    """前后台共用调用结算；未知用量保留预留，旧档位完成不推进新档位。"""
    entry = state["entries"][key]
    if not sink.get("_invoked"):
        del state["entries"][key]
    else:
        entry["seconds"] = max(0, elapsed)
        tokens = sink.get("total_tokens")
        if type(tokens) is int and tokens >= 0:
            entry.update(tokens=tokens, status="settled")
        else:
            entry["status"] = "unknown"
        policy, tier = state["policy"], state["tier"]
        if (entry["kind"] == "model" and entry["tier"] == tier
                and policy["action"] == "degrade" and tier < len(policy["tiers"])):
            used = [value for value in state["entries"].values()
                    if value["tier"] == tier and value["kind"] == "model"
                    and value["status"] != "not_invoked"]
            retired = state["history"].get("tiers", {}).get(str(tier), {"calls": 0, "tokens": 0})
            threshold = policy["tiers"][tier]
            if (failed or len(used) + retired["calls"] >= threshold["maxCalls"]
                    or sum(value["tokens"] for value in used) + retired["tokens"] >= threshold["maxTokens"]):
                state["tier"] += 1
                state["reason"] = reason
    compact_state(state)


class Account:
    def __init__(self, tenant_id, owner_id, session_id, policy):
        self.tenant_id, self.owner_id, self.session_id = tenant_id, owner_id, session_id
        self.policy = policy
        self.turn_id = None
        self.deadline = None
        self.timeout_handle = None
        self.expired = False

    async def locked(self, session):
        # 对会话主行取写锁，连首次建账也串行；外呼期间不持事务。
        result = await session.execute(update(ChatSession).where(
            ChatSession.id == self.session_id, ChatSession.tenant_id == self.tenant_id,
            ChatSession.owner_id == self.owner_id).values(updated_at=ChatSession.updated_at))
        if result.rowcount != 1:
            raise BudgetStopped("会话不可访问")
        row = await session.get(ConversationBudget, self.session_id)
        if row is None:
            history = list(await session.scalars(select(ObservabilityEvent).where(
                ObservabilityEvent.tenant_id == self.tenant_id,
                ObservabilityEvent.chat_id == str(self.session_id),
                ObservabilityEvent.category == "model_call")))
            historical = {"calls": len(history), "tokens": sum(r.total_tokens or 0 for r in history),
                          "unknown": sum(r.total_tokens is None for r in history),
                          "seconds": sum((r.duration_ms or 0) / 1000 for r in history)}
            previous = list(await session.scalars(select(ChatMessage.request_id).where(
                ChatMessage.tenant_id == self.tenant_id, ChatMessage.session_id == self.session_id,
                ChatMessage.role == "assistant")))
            observed_requests = {r.request_id for r in history if r.request_id}
            if any(not request_id or request_id not in observed_requests for request_id in previous):
                historical["unknown"] += 1
            row = ConversationBudget(session_id=self.session_id, tenant_id=self.tenant_id,
                                     owner_id=self.owner_id, state={"policy": self.policy, "history": historical,
                                                                  "tier": 0, "entries": {}, "reason": None})
            session.add(row)
        if row.tenant_id != self.tenant_id or row.owner_id != self.owner_id:
            raise BudgetStopped("预算归属不匹配")
        state = deepcopy(row.state)
        # 收紧立即生效；放宽仅影响新会话。
        for field in DEFAULTS:
            state["policy"][field] = min(state["policy"][field], self.policy[field])
        self.policy = deepcopy(state["policy"])
        return row, state

    @staticmethod
    def totals(state):
        entries = [e for e in state["entries"].values() if e["status"] != "not_invoked"]
        history = state["history"]
        elapsed = sum(turn["seconds"] + (max(0, time.time() - turn["started"]) if turn["status"] == "pending" else 0)
                      for turn in state.get("turns", {}).values())
        return {"calls": history["calls"] + len(entries),
                "tokens": history["tokens"] + sum(e["tokens"] for e in entries),
                "seconds": history["seconds"] + elapsed,
                "unknown": history["unknown"] + sum(e["status"] in {"pending", "unknown"} for e in entries)}

    async def start_turn(self, request_id):
        async with SessionLocal() as session:
            from eic.core import generation
            await generation.fence(session)
            row, state = await self.locked(session)
            now = await generation.database_now(session)
            for previous_id, previous in list(state.get("turns", {}).items()):
                if previous.get("status") != "pending":
                    continue
                lease = await session.scalar(select(GenerationLease).where(
                    GenerationLease.tenant_id == self.tenant_id,
                    GenerationLease.request_id == previous_id).limit(1))
                if lease is not None and (lease.state != "active" or lease.expires_at.replace(tzinfo=UTC) <= now):
                    message = await session.get(ChatMessage, lease.message_id)
                    if message is not None and message.session_id == self.session_id:
                        await close_abandoned_turn(session, message, ended_at=min(
                            now, lease.expires_at.replace(tzinfo=UTC)))
                        await session.flush()
                        row, state = await self.locked(session)
            turns = state.setdefault("turns", {})
            turn = turns.get(request_id)
            if turn and turn["status"] == "pending":
                raise BudgetStopped(BUDGET_HINTS["conversation_request_pending"], code="conversation_request_pending")
            if any(value["status"] == "pending" for value in turns.values()):
                raise BudgetStopped(BUDGET_HINTS["conversation_request_pending"], code="conversation_request_pending")
            remaining = state["policy"]["maxSeconds"] - self.totals(state)["seconds"]
            if remaining <= 0:
                await self.stop(session, row, state, "conversation_time_limit")
            owner = generation.OWNER.get()
            turns[request_id] = {"seconds": turn["seconds"] if turn else 0, "started": time.time(),
                                 "status": "pending", "limitSeconds": remaining,
                                 "ownerToken": owner[1] if owner else None}
            row.state = state
            await session.commit()
            self.turn_id = request_id
            # 仅当前进程使用单调时钟；持久恢复使用明确的 pending 状态。
            self.deadline = time.monotonic() + remaining
            task = asyncio.current_task()
            def expire():
                self.expired = True
                if not task.done():
                    task.cancel("conversation_budget_exceeded")
            self.timeout_handle = asyncio.get_running_loop().call_later(remaining, expire)

    async def finish_turn(self):
        if self.timeout_handle is not None:
            self.timeout_handle.cancel()
        if self.turn_id is None:
            return
        async with SessionLocal() as session:
            row, state = await self.locked(session)
            turn = state.get("turns", {}).get(self.turn_id)
            if turn is None:
                return  # 已结算且归并的旧轮次不再改写新轮次。
            from eic.core import generation
            owner = generation.OWNER.get()
            if turn["status"] == "pending" and turn.get("ownerToken") == (owner[1] if owner else None):
                turn["seconds"] += max(0, time.time() - turn["started"])
                turn["status"] = "settled"
                compact_state(state)
                row.state = state
                await session.commit()

    async def choose(self, primary):
        async with SessionLocal() as session:
            from eic.core import generation
            await generation.fence(session)
            row, state = await self.locked(session)
            policy = state["policy"]
            tier = state["tier"]
            if policy["action"] == "degrade" and policy["tiers"]:
                if tier >= len(policy["tiers"]):
                    await self.stop(session, row, state, "conversation_tiers_exhausted")
                chosen = int(policy["tiers"][tier]["modelId"])
            else:
                chosen = primary
            row.state = state
            await session.commit()
            return chosen

    async def stop(self, session, row, state, code, details=None):
        state.update(reason=BUDGET_HINTS[code], reasonCode=code, blocked=details or {})
        row.state = state
        await session.commit()
        raise BudgetStopped(state["reason"], code=code)

    async def reserve(self, model, kind, tokens):
        async with SessionLocal() as session:
            from eic.core import generation
            await generation.fence(session)
            row, state = await self.locked(session)
            total, policy = self.totals(state), state["policy"]
            if total["unknown"] and state["history"]["unknown"]:
                await self.stop(session, row, state, "conversation_usage_incomplete")
            details = {"kind": kind, "modelId": str(model.id), "requestedTokens": tokens,
                       "remainingTokens": max(0, policy["maxTokens"] - total["tokens"]),
                       "estimator": "model_limit" if kind == "model" else "utf8_bytes_plus_framing",
                       "exactTokenizer": False}
            if total["calls"] >= policy["maxCalls"]:
                await self.stop(session, row, state, "conversation_call_limit", details)
            if total["tokens"] + tokens > policy["maxTokens"]:
                await self.stop(session, row, state, "conversation_token_limit", details)
            if total["seconds"] >= policy["maxSeconds"]:
                await self.stop(session, row, state, "conversation_time_limit", details)
            key = uuid.uuid4().hex
            timeout = min(policy["callTimeoutSeconds"], policy["maxSeconds"] - total["seconds"])
            state["entries"][key] = {"kind": kind, "modelId": str(model.id), "tier": state["tier"],
                                      "tokens": tokens, "seconds": timeout, "status": "pending",
                                      "requestId": self.turn_id}
            state.update(reason=None, reasonCode=None, blocked=None)
            row.state = state
            await session.commit()
            return key, timeout

    async def settle(self, key, sink, elapsed, failed):
        async with SessionLocal() as session:
            row, state = await self.locked(session)
            settle_call_state(state, key, sink, elapsed, failed,
                              reason="当前档位失败或达到阈值，下次调用切换；已输出内容保留")
            row.state = state
            await session.commit()


def input_reservation(model, kind, inputs=(), kwargs=None):
    """文本按实际 UTF-8 字节保守预留；重排逐候选计入 query，不乘上下文窗口。"""
    kwargs = kwargs or {}
    if kind == "embedding":
        texts = inputs[0] if inputs else kwargs.get("texts")
        if texts is not None and not isinstance(texts, (str, bytes)):
            return 32 + sum(32 + text_units(text) for text in texts)
    elif kind == "rerank":
        query = inputs[0] if inputs else kwargs.get("query")
        hits = inputs[1] if len(inputs) > 1 else kwargs.get("hits")
        if isinstance(query, str) and hits is not None:
            return 32 + sum(32 + text_units(query) + text_units(str(hit.payload.get("content", "")))
                            for hit in hits)
    # 无法观察实际输入或协议隐含开销时沿用模型上限，不低估未知请求。
    count = max(1, len(inputs[0])) if kind == "embedding" and inputs else 1
    if kind == "rerank" and len(inputs) > 1:
        count = max(1, len(inputs[1]))
    return count * (model.max_input_tokens or 131072)


async def begin_call(model, kind, inputs=(), kwargs=None):
    account = ACTIVE.get()
    if account is None:
        return None
    if account.tenant_id != model.tenant_id:
        raise BudgetStopped("调用租户与预算归属不一致")
    tokens = input_reservation(model, kind, inputs, kwargs) + (min(model.max_output_tokens or 8192, account.policy["maxOutputTokens"]) if kind == "model" else 0)
    key, timeout = await account.reserve(model, kind, tokens)
    return account, key, timeout, time.monotonic()


async def finish_call(ticket, sink, failed):
    if ticket:
        account, key, _, started = ticket
        await account.settle(key, sink, time.monotonic() - started, failed)

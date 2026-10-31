"""真实 producer 的受控调用适配，不以缓存或未知结果作为健康探测成功。"""
import asyncio

from eic.ai.providers import ModelProviderError, chat_parameters
from eic.ai.stream_reader import complete_cleanup
from eic.ai.tool_center import ToolExecutionError, ToolValidationError
from eic.core.config import get_settings
from eic.core.dependency_controls import DependencyDenied, guard
from eic.core import observability


async def reliable_stream(factory, model, *, sessions, metadata):
    settings = get_settings()
    _, parameters = chat_parameters(model)
    reserve = parameters["inputLimit"] + parameters["outputLimit"]
    retries = settings.provider_rate_limit_retries if settings.dependency_controls_enabled else 0
    from eic.ai.runtime_policy import ACTIVE
    if ACTIVE.get() is not None:
        retries = 0
    for attempt in range(retries + 1):
        output_started = False
        try:
            async with guard(
                "provider", model.tenant_id, provider=model.provider, model_id=model.id,
                endpoint=model.endpoint, sessions=sessions, reserve_units=reserve,
                audit=metadata.setdefault("dependencyAttempts", []),
                failure_classifier=lambda exc: not (
                    isinstance(exc, ModelProviderError) and exc.reason in {
                        "output_budget_exceeded", "provider_parameters_rejected",
                    }
                ),
            ):
                timeout = settings.provider_total_timeout_seconds if settings.dependency_controls_enabled else None
                deadline_at = asyncio.get_running_loop().time() + timeout if timeout is not None else None
                metadata["providerInvocations"] = metadata.get("providerInvocations", 0) + 1
                context = observability.CONTEXT.get()
                previous = dict(context) if context is not None else None
                if context is not None:
                    context["model_attempt"] = attempt + 1
                source = None
                try:
                    source = factory()
                    while True:
                        try:
                            # SSE 的下一次读取可能由新 Task 驱动，超时不能跨 yield 绑定旧 Task。
                            async with asyncio.timeout_at(deadline_at) as deadline:
                                if context is not None:
                                    context["model_deadline"] = deadline
                                item = await anext(source)
                        except StopAsyncIteration:
                            break
                        output_started = output_started or bool(item)
                        yield item
                finally:
                    try:
                        if source is not None:
                            await complete_cleanup(source.aclose())
                    finally:
                        if context is not None:
                            for key in ("model_attempt", "model_deadline"):
                                if key in previous:
                                    context[key] = previous[key]
                                else:
                                    context.pop(key, None)
            return
        except DependencyDenied as exc:
            raise ModelProviderError(str(exc), reason=exc.code, retryable=exc.code == "circuit_open") from exc
        except TimeoutError as exc:
            raise ModelProviderError("模型整体调用超时，结果不确定，未自动重发",
                                     reason="provider_outcome_uncertain", retryable=False) from exc
        except ModelProviderError as exc:
            if (attempt < retries and exc.retryable and exc.reason == "provider_rate_limited"
                    and not output_started):
                metadata["retryCount"] = attempt + 1
                await asyncio.sleep(0.2)
                continue
            raise


def validate_tool_arguments(center, arguments, schema):
    try:
        center._validate_arguments(arguments, schema)
    except ToolValidationError as exc:
        raise ToolExecutionError("invalid_tool_arguments", f"工具参数校验失败：{exc}") from exc


async def reliable_tool(center, name, arguments, allowed, *, tenant_id, sessions, **kwargs):
    # 权限、审批和 Schema 拒绝不作为依赖健康失败。
    definition = center.get(name)
    if name not in allowed or definition is None or definition.requires_approval:
        return await center.execute_audited_async(name, arguments, allowed, **kwargs)
    validate_tool_arguments(center, arguments, definition.input_schema)
    try:
        async with guard(
            "tool", tenant_id, model_id=name, sessions=sessions,
            failure_classifier=lambda exc: not (
                isinstance(exc, ToolExecutionError) and exc.code in {
                    "invalid_tool_arguments", "invalid_tool_timeout", "approval_required",
                    "tool_not_allowed", "tool_budget_exceeded",
                }
            ),
        ) as observation:
            result = await center.execute_audited_async(name, arguments, allowed, **kwargs)
            observation["cached"] = result.replayed
            return result
    except DependencyDenied as exc:
        raise ToolExecutionError(exc.code, str(exc), effect_status="none") from exc

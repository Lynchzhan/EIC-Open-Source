"""纯观测字段投影；调用方负责可信来源，本模块不提供授权凭据。

不读取环境、数据库、网络或 ContextVar，不接收 Prompt 正文、图状态、
Task、ToolCallRequest 或租约。返回新字典，调用方在原协程 set/reset。
"""
from __future__ import annotations

import re
import uuid


_TAG = re.compile(r"[A-Za-z0-9_./:{}-]{1,120}")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _tag(value, limit=120):
    return (value if type(value) is str and len(value) <= limit
            and _TAG.fullmatch(value) else None)


def _id(value):
    return value if type(value) is int and 0 < value <= 2**63 - 1 else None


def turn_context(*, tenant_id, request_id, trace_id=None, run_id=None,
                 chat_id=None, owner_key=None):
    """从已校验的业务身份生成独立轮次上下文；无效身份返回空字典。

    request_id 必须来自持久请求或可信 worker 闭包。显式持久 trace 无效时
    丢弃本次观测，不替换为新 trace。UUID 与 observability.turn_* 一致，
    属于 EIC 来源 ID，不是 OTel span ID。可选字段无效时省略。
    """
    tenant = _id(tenant_id)
    request = _tag(request_id, 80)
    if tenant is None or request is None:
        return {}
    trace = (_tag(trace_id, 64) if trace_id is not None else
             uuid.uuid5(uuid.NAMESPACE_URL, f"eic:turn:{tenant}:{request}").hex)
    if trace is None:
        return {}
    result = {"tenant_id": tenant, "request_id": request, "trace_id": trace,
              "span_id": uuid.uuid5(
                  uuid.NAMESPACE_URL, f"eic:span:{tenant}:{request}").hex}
    for key, value, limit in (("run_id", run_id, 64), ("chat_id", chat_id, 80),
                              ("owner_key", owner_key, 64)):
        safe = _tag(value, limit)
        if safe is not None:
            result[key] = safe
    return result


def task_context(*, tenant_id, run_id, owner_key=None):
    """仅在 TaskRuntime.claim 成功后传入持久 TaskRun 身份。

    同一 TaskRun 重投不更换轮次；不使用 job_id、attempt、worker token。
    该合成 request_id 只关联观测，不写入业务请求或授权对象。
    """
    tenant, run = _id(tenant_id), _id(run_id)
    if tenant is None or run is None:
        return {}
    identity = f"task:{tenant}:{run}"
    return turn_context(tenant_id=tenant, request_id=identity,
                        run_id=identity, owner_key=owner_key)


def ingestion_context(*, tenant_id, document_id, run_id):
    """从已核对绑定的持久 Document/Run 身份投影独立入库轮次。

    调用方负责验证租户、文档与入库 run 的关联；格式校验不授予权限。
    同一 run 的解析与 Embedding 共用轮次，新 run 代表新的入库尝试。
    不读取队列 ctx，不附聊天身份，不操作 ContextVar 或业务对象。
    """
    tenant, document, run = _id(tenant_id), _id(document_id), _id(run_id)
    if tenant is None or document is None or run is None:
        return {}
    return turn_context(
        tenant_id=tenant, request_id=f"ingestion:{tenant}:{document}:{run}",
        run_id=f"ingestion:{run}")


def prompt_identity(*, prompt_id, release_version=None, release_digest=None):
    """投影已通过租户资源校验的本地 Prompt ID 和可选发布清单身份。

    默认策略只有 ID。可选 version/digest 必须来自独立、不可变、可信发布
    清单并已绑定实际使用版本；不能传 Resource.data 自报字段、updated_at、
    恢复用 promptDigest 或正文散列。格式检查不能证明来源可信。
    本地版本不是 Langfuse promptVersion，不建立远端 Prompt 关联。
    """
    identity = _id(prompt_id)
    if identity is None:
        return {}
    result = {"promptTemplateId": f"eic-prompt:{identity}",
              "promptIdentityStatus": "id_only"}
    version = _tag(release_version)
    digest = (release_digest if type(release_digest) is str
              and _DIGEST.fullmatch(release_digest) else None)
    if version is not None and digest is not None:
        result.update(promptTemplateVersion=version, promptTemplateDigest=digest,
                      promptIdentityStatus="release_manifest")
    return result


def prompt_summary(identities):
    """只接受有界本地 tuple；多模板不伪装为单一模板，不拼接或散列正文。

    仅用于可信 loader 产物；复制固定字段以避免任意 summary 透传。
    """
    if type(identities) is not tuple or len(identities) > 32:
        return {"promptIdentityStatus": "unavailable"}
    if not identities:
        return {"promptIdentityStatus": "none"}
    if len(identities) != 1:
        return {"promptIdentityStatus": "multiple_unversioned"}
    value = identities[0]
    if type(value) is not dict:
        return {"promptIdentityStatus": "unavailable"}
    identity = value.get("promptTemplateId")
    if (type(identity) is not str
            or re.fullmatch(r"eic-prompt:[1-9][0-9]{0,18}", identity) is None
            or _id(int(identity.split(":", 1)[1])) is None):
        return {"promptIdentityStatus": "unavailable"}
    result = prompt_identity(prompt_id=int(identity.split(":", 1)[1]))
    if value.get("promptIdentityStatus") == "release_manifest":
        result = prompt_identity(prompt_id=int(identity.split(":", 1)[1]),
            release_version=value.get("promptTemplateVersion"),
            release_digest=value.get("promptTemplateDigest"))
    return result

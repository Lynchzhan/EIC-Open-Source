"""本地授权查询结果的管理员外链与当前进程导出诊断。

调用方必须使用 observability_query.query_context 和其 criteria 查询所得的 ORM 行；
不得用请求参数、客户端 JSON 或未授权的事件集合构造 context/rows。
"""
from __future__ import annotations

import ipaddress
import os
import re
from urllib.parse import quote, urlsplit

from fastapi import HTTPException

from eic.core.config import get_settings


def _admin(context):
    return context.get("admin") is True


def _ui_base(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or "\\" in value:
        return None
    try:
        url = urlsplit(value)
        if not url.hostname or url.username is not None or url.password is not None:
            return None
        if url.query or url.fragment or url.path not in ("", "/"):
            return None
        _ = url.port
        loopback = url.hostname == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(url.hostname).is_loopback
        except ValueError:
            pass
        if url.scheme != "https" and not (url.scheme == "http" and loopback):
            return None
    except ValueError:
        return None
    return value.rstrip("/")


def trace_details(context, rows):
    """仅为已授权且同租户、同 Trace 的本地 ORM 记录构建地址。

    非管理员固定返回 None；路由必须省略 langfuse 字段，不读取项目配置。
    """
    if not _admin(context):
        return None
    unavailable = {"available": False, "state": "unavailable"}
    principal = context["principal"]
    if not rows:
        return unavailable
    first = rows[0]
    if not first.trace_id or any(
        row.tenant_id != principal.tenant_id or row.project != "eic"
        or row.trace_id != first.trace_id for row in rows
    ):
        return unavailable
    try:
        settings = get_settings()
        if getattr(settings, "langfuse_enabled", False) is not True:
            return {"available": False, "state": "disabled"}
        base = _ui_base(getattr(settings, "langfuse_ui_base_url", ""))
        project = getattr(settings, "langfuse_project_id", "")
        if not base or not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", project):
            return unavailable
        # 与 A 的公共 ingestion ID 映射完全复用，不从事件 summary 接受 URL。
        from eic.core.langfuse_contract import trace_identity

        trace_id = trace_identity(first, environment=settings.environment,
                                  namespace=settings.langfuse_namespace)
        if not isinstance(trace_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", trace_id):
            return unavailable
        if os.environ.get('EIC_LANGFUSE_TRANSPORT') == 'otlp_v4':
            from eic.core.langfuse_otlp import otlp_trace_id

            trace_id = otlp_trace_id(trace_id)
        return {"available": True, "state": "configured",
                "url": f"{base}/project/{quote(project, safe='')}/traces/{quote(trace_id, safe='')}"}
    except Exception:
        # 不把配置、原始异常或凭据带入响应；地址可构造不代表导出成功。
        return unavailable


def exporter_health(context, health):
    """health 必须为本 Web 进程 exporter 单例的同步 health 方法。

    不读取配置推断健康、不创建 exporter、不请求远端、不返回跨租户计数。
    B 的 enabled/running 未提供时明确 unknown，不能把 running 当投递确认。
    """
    if not _admin(context):
        raise HTTPException(403, detail={"code": "observation_health_forbidden",
                                        "errMsg": "仅管理员可读取导出进程诊断"})
    result = {"scope": "current_process", "source": "exporter.health",
              "workerStatus": "unknown",
              "state": "unknown", "deliveryConfirmed": False}
    try:
        snapshot = health()
        if not isinstance(snapshot, dict):
            return result
        enabled, running = snapshot.get("enabled"), snapshot.get("running")
        if enabled is False:
            result["state"] = "disabled"
        elif enabled is True and running is True:
            result["state"] = "running"
        elif enabled is True and running is False:
            result["state"] = "stopped"
    except Exception:
        pass
    return result

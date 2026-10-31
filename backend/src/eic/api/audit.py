"""audit 领域；通过显式装配提供跨域依赖。"""
from dataclasses import dataclass
from collections.abc import Callable
from types import SimpleNamespace as _RouteExports
from typing import Any
from eic.api.runtime import RuntimeBindings
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Request
from eic.models import Resource
from datetime import UTC
from datetime import datetime
import json


@dataclass(frozen=True)
class Dependencies:
    new_id: Callable[..., Any]
    SENSITIVE_LOG_KEYS: Any


def bind_routes(*, dependencies: Dependencies, runtime: RuntimeBindings):
    def sensitive_log_key(value: Any) -> str:
        return str(value).casefold().replace("_", "").replace("-", "")


    def redact_for_log(value: Any) -> Any:
        if isinstance(value, dict):
            blocked = {sensitive_log_key(item) for item in dependencies.SENSITIVE_LOG_KEYS}
            return {key: "***" if sensitive_log_key(key) in blocked else redact_for_log(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_for_log(item) for item in value]
        return value


    def request_ip(request: Request | None) -> str:
        if request is None:
            return ""
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
        return request.client.host if request.client else ""


    def request_device(request: Request | None) -> str:
        if request is None:
            return ""
        return (request.headers.get("user-agent") or "")[:240]


    async def attach_operation_logs(
        session: AsyncSession,
        *,
        tenant_id: int,
        user_id: int | None,
        username: str,
        operate_type: str,
        operate_remark: str,
        table: str,
        table_id: int | None = None,
        payload: Any = None,
        request: Request | None = None,
    ) -> None:
        if table in {"http_logs", "os_logs"}:
            return
        now = datetime.now(UTC).isoformat()
        ip = request_ip(request)
        device = request_device(request)
        req_path = request.url.path if request is not None else ""
        method = request.method if request is not None else ""
        body = json.dumps(redact_for_log(payload), ensure_ascii=False)[:2000] if payload is not None else ""
        session.add(Resource(
            id=await dependencies.new_id(session), tenant_id=tenant_id, resource_type="http_logs",
            name=operate_type[:120], owner_id=user_id,
            data={
                "userName": username, "operateType": operate_type, "operateRemark": operate_remark,
                "ip": ip, "device": device, "httpUrl": req_path, "httpBody": body,
                "httpMethod": method, "httpAction": req_path, "createTime": now,
            },
        ))
        session.add(Resource(
            id=await dependencies.new_id(session), tenant_id=tenant_id, resource_type="os_logs",
            name=operate_type[:120], owner_id=user_id,
            data={
                "createTime": now, "table": table, "tableId": table_id or 0, "sign": operate_type,
                "content": operate_remark, "actionUserName": username, "ipAddress": ip, "deviceMark": device,
            },
        ))

    return _RouteExports(
        sensitive_log_key=sensitive_log_key,
        redact_for_log=redact_for_log,
        request_ip=request_ip,
        request_device=request_device,
        attach_operation_logs=attach_operation_logs,
    )

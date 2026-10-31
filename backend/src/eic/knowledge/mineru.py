"""私有 MinerU 4.0 V1 adapter，不使用公共 API、远程 URL 或 ZIP 解压。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from eic.knowledge.documents import ExtractedDocument

UPSTREAM_COMMIT = "31e4e3d0a54e5a82d169bdd81b2f3eddb652bb27"
UPSTREAM_VERSION = "4.0.11"
LOGGER = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PRIVATE = tuple(ipaddress.ip_network(value) for value in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "fc00::/7", "::1/128",
))


class MinerUError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__("mineru:" + code)


def private_endpoint(settings) -> str:
    try:
        raw = settings.mineru_endpoint
        if not raw or any(ord(char) < 33 for char in raw) or "\\" in raw:
            raise ValueError
        url = urlsplit(raw)
        if (url.scheme not in {"http", "https"} or url.username or url.password
                or url.query or url.fragment or url.path not in {"", "/"}):
            raise ValueError
        address = ipaddress.ip_address(url.hostname or "")
        allowed = {ipaddress.ip_address(item.strip()) for item in settings.mineru_allowed_ips.split(",")
                   if item.strip()}
        if (address not in allowed or not any(address in network for network in _PRIVATE)
                or address.is_link_local or address.is_multicast or address.is_unspecified):
            raise ValueError
        port = url.port or (443 if url.scheme == "https" else 80)
        host = f"[{address}]" if address.version == 6 else str(address)
        return f"{url.scheme}://{host}:{port}"
    except ValueError:
        raise MinerUError("endpoint_rejected") from None


def extraction_parameters(settings) -> dict:
    # 关闭时不改变旧索引指纹；不把密钥或原始地址写入运行报告。
    if settings.pdf_extractor != "mineru":
        return {}
    try:
        endpoint = private_endpoint(settings)
    except MinerUError:
        endpoint = settings.mineru_endpoint
    return {"adapter": "mineru_v1", "upstreamCommit": UPSTREAM_COMMIT,
            "endpointHash": hashlib.sha256(endpoint.encode()).hexdigest(),
            "tier": settings.mineru_tier, "ocrMode": settings.mineru_ocr_mode,
            "fallbackLocal": settings.mineru_fallback_local,
            "maxPdfBytes": settings.mineru_max_pdf_bytes,
            "maxResponseBytes": settings.mineru_max_response_bytes,
            "maxTextChars": settings.mineru_max_text_chars, "maxPages": settings.mineru_max_pages,
            "timeoutSeconds": settings.mineru_timeout_seconds,
            "maxPolls": settings.mineru_max_polls,
            "pollIntervalSeconds": settings.mineru_poll_interval_seconds}


def identifier(value, prefix: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix) or not _ID.fullmatch(value):
        raise MinerUError("invalid_identifier")
    return value


def decode_json(payload: bytes):
    try:
        return json.loads(payload)
    except (ValueError, UnicodeError, RecursionError):
        raise MinerUError("invalid_json") from None


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "tr":
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append(" | ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _spans(value) -> str:
    if not isinstance(value, list):
        raise MinerUError("invalid_structured_content")
    parts = []
    for span in value:
        if (not isinstance(span, dict) or span.get("type") not in {
                "text", "equation_inline", "code_inline", "hyperlink"}
                or not isinstance(span.get("content"), str)):
            raise MinerUError("invalid_structured_content")
        parts.append(span["content"])
    return "".join(parts)


def _block_text(item: dict) -> str:
    kind, content = item.get("type"), item.get("content")
    if not isinstance(content, dict):
        raise MinerUError("invalid_structured_content")
    if kind in {"paragraph", "title", "page_footnote", "page_header", "page_footer",
                "page_number", "page_aside_text"}:
        key = {"paragraph": "paragraph_content", "title": "title_content"}.get(kind, kind + "_content")
        return _spans(content.get(key))
    if kind in {"list", "index"}:
        values = content.get("list_items")
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise MinerUError("invalid_structured_content")
        return "\n".join(_spans(value.get("item_content")) for value in values)
    if kind == "equation_interline":
        value = content.get("math_content")
        if not isinstance(value, str):
            raise MinerUError("invalid_structured_content")
        return value
    if kind in {"image", "chart", "table", "code", "algorithm"}:
        parts = []
        for key in (kind + "_caption", kind + "_footnote"):
            if key in content:
                parts.append(_spans(content[key]))
        if kind in {"code", "algorithm"}:
            parts.append(_spans(content.get(kind + "_content")))
        else:
            value = content.get("html" if kind == "table" else "content", "")
            if not isinstance(value, str):
                raise MinerUError("invalid_structured_content")
            parser = _TableText()
            parser.feed(value)
            parts.append("".join(parser.parts).strip())
        return "\n".join(part for part in parts if part.strip())
    raise MinerUError("unsupported_block")


def structured_document(payload: bytes, page_count: int, settings) -> ExtractedDocument:
    pages = decode_json(payload)
    if not isinstance(pages, list) or len(pages) != page_count:
        raise MinerUError("page_count_mismatch")
    parts, failed, tables, images = [], [], 0, 0
    trusted_pages = []
    length = 0
    for number, blocks in enumerate(pages, 1):
        if not isinstance(blocks, list):
            raise MinerUError("invalid_structured_content")
        texts = []
        for item in blocks:
            if not isinstance(item, dict):
                raise MinerUError("invalid_structured_content")
            text = _block_text(item).strip()
            # 只允许 adapter 产生可信页标记，文档内容中的同形字符串转为普通文字。
            text = re.sub(r"第(\d+)页", r"第 \1 页", text)
            length += len(text)
            if length > settings.mineru_max_text_chars:
                raise MinerUError("text_too_large")
            tables += item.get("type") == "table"
            images += item.get("type") == "image"
            if text:
                texts.append(text)
        if texts:
            parts.append(f"[第{number}页]\n" + "\n".join(texts))
            trusted_pages.append((number, parts[-1]))
        else:
            failed.append(number)
    if not parts:
        raise MinerUError("empty_document")
    return ExtractedDocument("\n\n".join(parts), {
        "methods": ["mineru_v1"], "failedPages": failed, "pageCount": page_count,
        "imageCount": images, "tableCount": tables,
        "notes": "MinerU 私有接口抽取；空页仅记录，不推断 OCR 质量。",
        "extractor": {"requested": "mineru", "used": "mineru", "status": "completed",
                      "upstreamCommit": UPSTREAM_COMMIT, "parserVersion": UPSTREAM_VERSION,
                      "tier": settings.mineru_tier},
    }, pages=tuple(trusted_pages))


def _read_pdf(path: Path, settings) -> tuple[bytes, int]:
    import pymupdf
    with path.open("rb") as stream:
        payload = stream.read(settings.mineru_max_pdf_bytes + 1)
    if len(payload) > settings.mineru_max_pdf_bytes:
        raise MinerUError("pdf_too_large")
    if not payload.startswith(b"%PDF-"):
        raise MinerUError("invalid_pdf")
    try:
        with pymupdf.open(stream=payload, filetype="pdf") as document:
            count = document.page_count
            if document.needs_pass or not 1 <= count <= settings.mineru_max_pages:
                raise MinerUError("pdf_pages_rejected")
    except MinerUError:
        raise
    except Exception:  # noqa: BLE001 - PyMuPDF 异常类型不稳定，统一脱敏成固定错误码。
        raise MinerUError("invalid_pdf") from None
    return payload, count


async def _request(client, endpoint, key, method, route, limit, timeout, **kwargs):
    headers = {"Accept-Encoding": "identity"}
    if "content" in kwargs:
        headers["Content-Type"] = "application/pdf"
    if key:
        headers["Authorization"] = "Bearer " + key
    async with client.stream(method, endpoint + route, headers=headers,
                             timeout=timeout, follow_redirects=False, **kwargs) as response:
        if response.status_code not in {200, 202}:
            raise MinerUError("http_" + str(response.status_code))
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise MinerUError("encoding_rejected")
        declared = response.headers.get("content-length")
        if declared and (not declared.isdecimal() or int(declared) > limit):
            raise MinerUError("response_too_large")
        if "zip" in response.headers.get("content-type", "").lower():
            raise MinerUError("archive_rejected")
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            body.extend(chunk)
            if len(body) > limit:
                raise MinerUError("response_too_large")
        if body[:2] == b"PK":
            raise MinerUError("archive_rejected")
        return bytes(body)


async def _parse(client, endpoint, settings, payload, page_count, state):
    key = settings.mineru_api_key.get_secret_value()
    limit, timeout = settings.mineru_max_response_bytes, min(settings.mineru_timeout_seconds, 30)

    async def request(method, route, **kwargs):
        remaining = limit - state.get("responseBytes", 0)
        if remaining <= 0:
            raise MinerUError("response_too_large")
        response = await _request(client, endpoint, key, method, route, remaining, timeout, **kwargs)
        state["responseBytes"] = state.get("responseBytes", 0) + len(response)
        return response

    upload = decode_json(await request("POST", "/v1/uploads", json={
        "filename": "document.pdf", "bytes": len(payload), "mime_type": "application/pdf",
        "purpose": "parse", "expires_after": {"anchor": "created_at", "seconds": 3600}}))
    if not isinstance(upload, dict):
        raise MinerUError("invalid_upload")
    upload_id = identifier(upload.get("id"), "upload_")
    state["uploadId"] = upload_id
    # 不请求秒传，不复用其他用户文件；不执行服务器返回的任意地址或上传头。
    if (upload.get("status") != "pending" or upload.get("upload_method") != "PUT"
            or upload.get("upload_headers") not in (None, {}, {"Content-Type": "application/pdf"})
            or upload.get("upload_url") not in {
                f"/v1/uploads/{upload_id}/content", endpoint + f"/v1/uploads/{upload_id}/content"}):
        raise MinerUError("upload_target_rejected")
    await request("PUT", f"/v1/uploads/{upload_id}/content", content=payload)
    complete = decode_json(await request("POST", f"/v1/uploads/{upload_id}/complete",
                                        json={"sha256sum": hashlib.sha256(payload).hexdigest()}))
    if (not isinstance(complete, dict) or complete.get("id") != upload_id
            or complete.get("status") != "completed" or not isinstance(complete.get("file"), dict)):
        raise MinerUError("invalid_upload_complete")
    source_id = identifier(complete["file"].get("id"), "file-")
    state["sourceId"] = source_id
    job = decode_json(await request("POST", "/v1/parse/jobs", json={
        "files": [{"source": {"type": "file_id", "file_id": source_id}}],
        "tier": settings.mineru_tier, "ocr_mode": settings.mineru_ocr_mode,
        "output_formats": ["structured_content"]}))
    if not isinstance(job, dict):
        raise MinerUError("invalid_job")
    job_id = identifier(job.get("job_id"), "job_")
    state["jobId"] = job_id
    for poll in range(settings.mineru_max_polls + 1):
        if not isinstance(job, dict) or job.get("job_id") != job_id:
            raise MinerUError("invalid_job")
        status = job.get("status")
        state["jobStatus"] = status
        if status == "completed":
            break
        if status not in {"queued", "running"}:
            raise MinerUError("job_not_completed")
        if poll == settings.mineru_max_polls:
            raise MinerUError("poll_exhausted")
        await asyncio.sleep(settings.mineru_poll_interval_seconds)
        job = decode_json(await request("GET", f"/v1/parse/jobs/{job_id}"))
    files = job.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise MinerUError("invalid_job_files")
    result = files[0]
    if result.get("file_id") != source_id:
        raise MinerUError("result_mismatch")
    outputs = result.get("output_files")
    artifact = outputs.get("structured_content") if isinstance(outputs, dict) else None
    if isinstance(artifact, dict):
        state["outputId"] = identifier(artifact.get("file_id"), "file-")
    if (result.get("status") != "completed"
            or (result.get("parse") or {}).get("parser_version") != UPSTREAM_VERSION
            or result.get("page_range") != ("1" if page_count == 1 else f"1-{page_count}")):
        raise MinerUError("result_mismatch")
    if not isinstance(artifact, dict):
        raise MinerUError("missing_structured_content")
    output_id = identifier(artifact.get("file_id"), "file-")
    state["outputId"] = output_id
    size = artifact.get("bytes")
    if type(size) is not int or not 0 < size <= limit:
        raise MinerUError("response_too_large")
    content = await request("GET", f"/v1/files/{output_id}/content")
    if len(content) != size:
        raise MinerUError("artifact_size_mismatch")
    return structured_document(content, page_count, settings)


async def _cleanup(client, endpoint, settings, state):
    result = {"status": "not_created", "blobDeletion": "not_proven"}
    if not state:
        return result
    actions = []
    if state.get("jobId") and state.get("jobStatus") not in {"completed", "partial", "failed", "canceled"}:
        actions.append(("DELETE", f"/v1/parse/jobs/{state['jobId']}"))
    for key in ("outputId", "sourceId"):
        if state.get(key):
            actions.append(("DELETE", f"/v1/files/{state[key]}"))
    if state.get("uploadId") and not state.get("sourceId"):
        actions.append(("POST", f"/v1/uploads/{state['uploadId']}/cancel"))
    if not actions:
        return result
    pending = False
    try:
        async with asyncio.timeout(5):
            for method, route in actions:
                try:
                    response = decode_json(await _request(client, endpoint,
                        settings.mineru_api_key.get_secret_value(), method, route, 4096, 2))
                    if (not isinstance(response, dict) or
                            ("/files/" in route and response.get("deleted") is not True) or
                            ("/parse/jobs/" in route and response.get("status") != "canceled") or
                            ("/uploads/" in route and response.get("status") != "cancelled")):
                        raise MinerUError("cleanup_response_invalid")
                except Exception:  # noqa: BLE001 - 单项清理失败仍尝试剩余资源，不记录响应或凭据。
                    pending = True
    except Exception:  # noqa: BLE001 - 有界清理只记录 pending，不掩盖主请求的结果。
        pending = True
    result["status"] = "cleanup_pending" if pending else "api_resources_released"
    if pending:
        LOGGER.warning("MinerU cleanup pending; sidecar retention policy required")
    return result


async def extract_pdf(path: Path, settings, *, transport=None) -> ExtractedDocument:
    endpoint = private_endpoint(settings)
    try:
        payload, count = await asyncio.to_thread(_read_pdf, path, settings)
    except MinerUError:
        raise
    except OSError:
        raise MinerUError("source_unavailable") from None
    state, cleanup = {}, {"status": "not_created", "blobDeletion": "not_proven"}
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                                     follow_redirects=False) as client:
            try:
                async with asyncio.timeout(settings.mineru_timeout_seconds):
                    extracted = await _parse(client, endpoint, settings, payload, count, state)
            finally:
                cleanup = await _cleanup(client, endpoint, settings, state)
        return ExtractedDocument(extracted.text, {**extracted.report,
            "extractor": {**extracted.report["extractor"], "cleanup": cleanup}}, pages=extracted.pages)
    except MinerUError as exc:
        exc.cleanup = cleanup
        raise
    except asyncio.CancelledError as exc:
        exc.cleanup = cleanup
        raise
    except (TimeoutError, httpx.TimeoutException):
        error = MinerUError("timeout")
    except httpx.HTTPError:
        error = MinerUError("transport_error")
    except Exception:  # noqa: BLE001 - 协议异常不把私有响应、URL 或凭据带到 worker 日志。
        error = MinerUError("protocol_error")
    error.cleanup = cleanup
    raise error from None


def failure_report(error, *, status="failed"):
    return {"methods": [], "notes": "MinerU 抽取未完成；不视为 OCR 成功。",
            "extractor": {"requested": "mineru", "used": "none", "status": status,
                          "errorCode": getattr(error, "code", "interrupted"), "automaticRetry": False,
                          "upstreamCommit": UPSTREAM_COMMIT,
                          "cleanup": getattr(error, "cleanup", {"status": "not_created",
                                                                  "blobDeletion": "not_proven"})}}


async def extract_for_ingestion(path: Path, *, settings=None, local_extractor=None):
    from eic.core.config import get_settings
    from eic.knowledge.documents import extract_document
    settings = settings or get_settings()
    local_extractor = local_extractor or extract_document
    from eic.knowledge.local_parser import extract_local
    if path.suffix.lower() != ".pdf" or settings.pdf_extractor == "local":
        return await extract_local(path, settings, local_extractor)
    try:
        return await extract_pdf(path, settings)
    except asyncio.CancelledError as exc:
        exc.mineru_report = failure_report(exc, status="interrupted")
        raise
    except MinerUError as exc:
        exc.mineru_report = failure_report(exc)
        LOGGER.warning("MinerU extraction failed code=%s fallback=%s", exc.code,
                       settings.mineru_fallback_local)
        if not settings.mineru_fallback_local:
            raise
        try:
            local = await extract_local(path, settings, local_extractor)
        except Exception:  # noqa: BLE001 - 复用既有 extractor，失败必须落库且不泄露原始异常。
            LOGGER.error("MinerU local fallback failed code=%s", exc.code)
            error = MinerUError("local_fallback_failed")
            error.cleanup = getattr(exc, "cleanup", {"status": "not_created", "blobDeletion": "not_proven"})
            error.mineru_report = failure_report(error)
            error.mineru_report["extractor"]["upstreamErrorCode"] = exc.code
            raise error from None
        if not local.text.strip():
            error = MinerUError("local_fallback_empty")
            error.cleanup = getattr(exc, "cleanup", {"status": "not_created", "blobDeletion": "not_proven"})
            error.mineru_report = failure_report(error)
            error.mineru_report["extractor"]["upstreamErrorCode"] = exc.code
            raise error
        report = {**local.report, "notes": (local.report.get("notes", "") +
                  f" MinerU 失败（{exc.code}）；已回退本地抽取，非 MinerU 成功。").strip(),
                  "extractor": {"requested": "mineru", "used": "local", "status": "fallback",
                                "errorCode": exc.code, "automaticRetry": False,
                                "cleanup": getattr(exc, "cleanup", {"status": "not_created"}),
                                "upstreamCommit": UPSTREAM_COMMIT}}
        return ExtractedDocument(local.text, report, pages=local.pages)

"""Migrate local or reachable remote files from an exported TFile record file.

Input may be a JSON array or CSV with file_id, tenant_id, name, path/original_path,
and url/source_url. Existing targets are never overwritten; sources are retained.
Secure publication requires directory-relative, no-follow filesystem operations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIELDS = ["file_id", "original_name", "original_path", "source_url", "target_path", "sha256", "size", "status", "error_message"]
SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def positive_id(raw) -> str:
    if type(raw) is int:
        raw = str(raw)
    if not isinstance(raw, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", raw):
        raise ValueError("invalid_file_scope_id")
    if int(raw) >= 2**63:
        raise ValueError("invalid_file_scope_id")
    return raw


def scope_id(record, *aliases) -> str:
    values = [positive_id(record[key]) for key in aliases if key in record]
    if not values or len(set(values)) != 1:
        raise ValueError("ambiguous_file_scope_id")
    return values[0]


def require_secure_target_platform() -> None:
    required = (os.open, os.mkdir, os.stat, os.unlink)
    if (not all(operation in os.supports_dir_fd for operation in required)
            or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")):
        raise ValueError("secure_target_platform_unsupported")


@contextmanager
def target_directory(root: Path, relative: Path):
    """逐级固定目录句柄；不支持安全操作的平台必须拒绝，不能回退到路径写入。"""
    require_secure_target_platform()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors = []
    try:
        descriptor = os.open(root.anchor, flags)
        descriptors.append(descriptor)
        for part in (*root.parts[1:], *relative.parts):
            if part in {"", ".", ".."}:
                raise ValueError("invalid_target_component")
            try:
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
            except FileExistsError:
                pass
            descriptor = os.open(part, flags, dir_fd=descriptor)
            descriptors.append(descriptor)
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def publish_file(source: Path, parent_fd: int, name: str) -> None:
    # O_EXCL 同时拒绝已存在的普通文件、硬链接和符号链接，绝不打开旧目标。
    descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent_fd)
    identity = None
    try:
        identity = os.fstat(descriptor)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = None
            with source.open("rb") as incoming:
                shutil.copyfileobj(incoming, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
    except BaseException:
        # 只移除本次创建且身份未变的半成品，不清理来源或任何旧文件。
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if identity is not None and (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                os.unlink(name, dir_fd=parent_fd)
        except OSError:
            pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_records(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError("input JSON must be an array")
        if any(not isinstance(row, dict) for row in value):
            raise ValueError("input JSON rows must be objects")
        return [{str(key): "" if item is None else str(item) for key, item in row.items()} for row in value]
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def safe_name(value: str, fallback: str) -> str:
    name = SAFE_NAME.sub("_", Path(value or fallback).name).strip(" .")
    return name or fallback


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def download(url: str, destination: Path) -> None:
    request = Request(url, headers={"User-Agent": "Enterprise-Intelligence-Center-file-migrator/1.0"})
    with urlopen(request, timeout=30) as response, destination.open("wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 1024)


def migrate_one(record: dict[str, str], target_root: Path) -> dict[str, str | int]:
    require_secure_target_platform()
    file_id = scope_id(record, "file_id", "id")
    tenant_id = scope_id(record, "tenant_id", "tenantId")
    original_name = record.get("original_name") or record.get("name") or f"{file_id}.bin"
    root = Path(os.path.abspath(target_root))
    relative = Path(tenant_id) / datetime.now(UTC).strftime("%Y/%m") / file_id
    target = root / relative / safe_name(original_name, f"{file_id}.bin")
    # resolve 检查归属与既有链接；之后还须通过 no-follow 目录句柄阻断检查/使用竞态。
    resolved_root = root.resolve()
    resolved_target = target.resolve()
    if (resolved_root != root or resolved_target != target
            or not resolved_target.is_relative_to(resolved_root)):
        raise ValueError("target_path_rejected")
    source_path = record.get("original_path") or record.get("path") or ""
    source_url = record.get("source_url") or record.get("url") or ""
    expected = (record.get("sha256") or record.get("hash") or "").lower()
    if expected and not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("invalid_source_hash")
    result: dict[str, str | int] = {field: "" for field in FIELDS}
    result.update({"file_id": file_id, "original_name": original_name, "original_path": source_path, "source_url": source_url, "target_path": str(target)})
    with tempfile.TemporaryDirectory(prefix="eic-file-") as temporary:
        temporary_path = Path(temporary) / target.name
        try:
            if source_path and Path(source_path).is_file():
                shutil.copy2(source_path, temporary_path)
            elif source_url:
                download(source_url, temporary_path)
            else:
                result["status"] = "MISSING"
                result["error_message"] = "no local path or remote URL"
                return result
            actual_hash, size = sha256_file(temporary_path)
            if expected and expected != actual_hash:
                result.update({"status": "HASH_MISMATCH", "sha256": actual_hash, "size": size, "error_message": "source hash does not match exported hash"})
                return result
            with target_directory(root, relative) as parent_fd:
                # 检查目录未被移动，发布操作仍仅通过已固定的父目录句柄。
                parent_identity = os.fstat(parent_fd)
                path_identity = target.parent.stat(follow_symlinks=False)
                if (target.resolve() != target or
                        (parent_identity.st_dev, parent_identity.st_ino) !=
                        (path_identity.st_dev, path_identity.st_ino)):
                    raise ValueError("target_path_changed")
                publish_file(temporary_path, parent_fd, target.name)
            result.update({"status": "SUCCESS", "sha256": actual_hash, "size": size})
        except FileExistsError:
            result["status"] = "TARGET_EXISTS"
            result["error_message"] = "existing_target_not_overwritten"
        except (OSError, HTTPError, URLError, ValueError) as exc:
            result["status"] = "DOWNLOAD_FAILED" if source_url else "MISSING"
            result["error_message"] = (str(exc) if type(exc) is ValueError and str(exc) in {
                "secure_target_platform_unsupported", "invalid_target_component", "target_path_changed",
            } else "file_copy_failed")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=Path(__file__).parents[1] / "reports" / "file_migration_report.csv")
    parser.add_argument("--missing-report", type=Path, default=Path(__file__).parents[1] / "reports" / "missing_files.csv")
    parser.add_argument("--hash-report", type=Path, default=Path(__file__).parents[1] / "reports" / "file_hashes.json")
    args = parser.parse_args()
    records = load_records(args.input)
    # 全部 ID 在第一次文件写入前检查；任何冲突都不靠覆盖来“恢复”。
    identities = [(scope_id(row, "tenant_id", "tenantId"), scope_id(row, "file_id", "id"))
                  for row in records]
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate_file_scope_id")
    rows = [migrate_one(record, args.target_root) for record in records]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    missing = [row for row in rows if row["status"] != "SUCCESS"]
    with args.missing_report.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(missing)
    args.hash_report.write_text(json.dumps({str(row["file_id"]): row["sha256"] for row in rows if row["sha256"]}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"total": len(rows), "success": len(rows) - len(missing), "unresolved": len(missing), "report": str(args.report)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

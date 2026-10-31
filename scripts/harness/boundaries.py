"""外部固定校验清单、四类边界和可复核的追加日志。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re


class HarnessError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise HarnessError("JSON 存在重复字段")
        result[key] = value
    return result


def read_json(data):
    return json.loads(data, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(HarnessError("JSON 含非有限数值")))


def safe_file(root, name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name
            or path.as_posix() != name):
        raise HarnessError("Harness 文件路径非法")
    root = Path(root).resolve()
    target = root
    for part in path.parts:
        target /= part
        if target.is_symlink():
            raise HarnessError("Harness 文件含符号链接")
    if not target.is_file() or not target.resolve().is_relative_to(root):
        raise HarnessError("Harness 文件缺失或越界")
    return target


def validate_lock(root, manifest_path, expected_sha256):
    data = Path(manifest_path).read_bytes()
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256) or digest(data) != expected_sha256:
        raise HarnessError("Locked 清单与外部批准哈希不一致")
    manifest = read_json(data)
    if manifest.get("schemaVersion") != 1 or set(manifest.get("surfaces", {})) != {
        "Locked", "Editable", "Append-only", "Human-controlled",
    }:
        raise HarnessError("Harness 边界清单无效")
    files = manifest.get("files", {})
    if not isinstance(files, dict) or not files:
        raise HarnessError("Locked 文件集合为空")
    for name, expected in files.items():
        if not re.fullmatch(r"[0-9a-f]{64}", expected) or digest(safe_file(root, name).read_bytes()) != expected:
            raise HarnessError("Locked 文件已改变：" + name)
    from release_policy import excluded_source_path
    prefixes = [name for name in manifest["surfaces"]["Locked"] if name.endswith("/")]
    for prefix in prefixes:
        for path in (Path(root) / prefix).rglob("*"):
            name = path.relative_to(root).as_posix()
            if name == "evals/LOCK.json" or excluded_source_path(name) or not path.is_file():
                continue
            if name not in files or path.is_symlink():
                raise HarnessError("Locked 目录出现未批准文件：" + name)
    return manifest


@contextmanager
def _journal(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise HarnessError("日志路径含符号链接")
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _scan_records(fd, *, collect):
    # 始终完整验链；append 仅保留计数与链头，read 仍返回完整记录。
    os.lseek(fd, 0, os.SEEK_SET)
    head, records, count = "0" * 64, [], 0
    with os.fdopen(os.dup(fd), "rb") as stream:
        for line in stream:
            if not line.endswith(b"\n"):
                raise HarnessError("日志尾部不完整，保留现场并拒绝追加")
            row = read_json(line)
            if set(row) != {"seq", "previous", "timestamp", "event", "recordHash"}:
                raise HarnessError("日志记录结构无效")
            body = {key: value for key, value in row.items() if key != "recordHash"}
            if row["seq"] != count + 1 or row["previous"] != head or digest(canonical(body).encode()) != row["recordHash"]:
                raise HarnessError("日志哈希链校验失败")
            head = row["recordHash"]
            count += 1
            if collect:
                records.append(row)
    return records, head, count


def _read_records(fd):
    records, head, _ = _scan_records(fd, collect=True)
    return records, head


def read_journal(path, *, expected_head=None):
    with _journal(path) as fd:
        records, head = _read_records(fd)
        if expected_head is not None and head != expected_head:
            raise HarnessError("日志与外部锚点不一致，可能被截断或替换")
        return records, head


def append_journal(path, event, *, expected_head=None):
    allowed = {"runId", "candidateSha256", "policySha256", "caseSetSha256", "passed",
               "failedCaseIds", "stage", "gate", "errorCode", "metricsSha256"}
    if not isinstance(event, dict) or set(event) - allowed or not event.get("runId"):
        raise HarnessError("日志只接收批准的无正文元数据")
    for key, value in event.items():
        if key.endswith("Sha256"):
            valid = isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        elif key == "passed":
            valid = type(value) is bool
        elif key == "failedCaseIds":
            valid = isinstance(value, list) and len(value) <= 1000 and all(
                isinstance(item, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", item) for item in value)
        else:
            valid = isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
        if not valid:
            raise HarnessError("日志元数据无效：" + key)
    with _journal(path) as fd:
        _, head, count = _scan_records(fd, collect=False)
        if expected_head is not None and head != expected_head:
            raise HarnessError("日志与外部锚点不一致")
        body = {"seq": count + 1, "previous": head,
                "timestamp": datetime.now(timezone.utc).isoformat(), "event": event}
        row = {**body, "recordHash": digest(canonical(body).encode())}
        data = (canonical(row) + "\n").encode()
        while data:
            data = data[os.write(fd, data):]
        os.fsync(fd)
        return row["recordHash"]

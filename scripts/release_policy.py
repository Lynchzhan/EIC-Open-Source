"""源代码归档的共同文件名策略；不是内容秘密扫描器。"""

import re
import os
import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath

SKIP_DIR_NAMES = {
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
    "uploads", "data", "ui-qa-20260911", "dist", "runtime", ".reviews",
    ".packages", ".cache", ".mypy_cache", ".tox", "backups", "logs", "secrets",
    "venv", ".hg", ".svn", ".claude", ".idea", "private",
}
SKIP_FILE_NAMES = {
    ".env", ".coverage", "eic.db", "enterprise-intelligence-center-cloud.tar.gz",
    "enterprise-intelligence-center-cloud.zip", "admin-password.txt",
}
SKIP_SUFFIXES = {
    ".pyc", ".pyo", ".log", ".db", ".sqlite", ".sqlite3", ".sql", ".dump", ".rdb",
    ".bak", ".backup", ".old", ".pem", ".key", ".p12", ".pfx", ".jks",
    ".keystore", ".zip", ".gz", ".tar", ".7z",
}
PUBLIC_SCHEMA_FILES = {
    "backend/migrations/legacy_cloud_lite.sql",
    "deploy/postgres-init/001-vector.sql",
    "deploy/postgres-init/002-memory.sql",
}


def excluded_source_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or "\\" in relative or ":" in relative:
        return True
    parts = tuple(part.casefold() for part in path.parts)
    if not parts or any(part in SKIP_DIR_NAMES for part in parts):
        return True
    # 明确列出的建表脚本属于公开源码；其他 SQL 默认视为可能的数据库导出。
    if relative in PUBLIC_SCHEMA_FILES:
        return False
    # 迁移结果、捕获样例、恢复数据和源代码分开发布。
    if len(parts) >= 2 and parts[:2] == ("migration", "reports"):
        return True
    name = path.name.casefold()
    if name.endswith(".env") or (".env." in name and not name.endswith(".env.example")):
        return True
    if name == ".env" or (name.startswith(".env.") and name != ".env.example"):
        return True
    if name in SKIP_FILE_NAMES or path.suffix.casefold() in SKIP_SUFFIXES:
        return True
    if re.search(r"\.(?:db|sqlite3?)(?:-wal|-shm|-journal)$", name):
        return True
    if re.fullmatch(r"(?:id_rsa|id_ed25519|credentials)(?:\..*)?", name):
        return True
    if re.search(r"(?:admin|bootstrap).*password.*\.txt$", name):
        return True
    return bool(re.search(r"(?:secret|token|credential)(?:s)?\.(?:txt|json|ya?ml|conf)$", name))


def public_source_sha256(root: Path) -> str:
    """目录剪枝但保持原成员口径、排序及符号链接拒绝规则。"""
    root = Path(root)
    members = {}
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                # 只有后代也被排除时才剪枝；文件后缀规则不自动排除同名目录的后代。
                if excluded_source_path(name) and (
                    not entry.is_dir(follow_symlinks=False)
                    or excluded_source_path(name + "/__source_member__.py")
                ):
                    continue
                if entry.is_symlink():
                    raise ValueError("源码成员含符号链接")
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    members[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(json.dumps(
        members, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()

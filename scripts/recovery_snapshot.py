"""P7 合成恢复演练：SQLite + uploads + 配置/密钥；只恢复到新的具名目录。"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import time


def bound(path, scope):
    path, scope = Path(path).absolute(), Path(scope).resolve()
    # 拒绝路径链中的符号链接，避免通过已有目录重定向到其他项目。
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise ValueError("恢复路径含符号链接")
    path = path.resolve()
    if path == scope or not path.is_relative_to(scope):
        raise ValueError("操作路径不在明确的演练根目录内")
    return path


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inventory(root):
    files = {}
    database = root / "eic.db"
    if database.is_symlink() or not database.is_file() or not database.resolve().is_relative_to(root):
        raise ValueError("备份数据库不存在或含链接")
    for prefix in ("uploads", "config"):
        base = root / prefix
        if base.is_symlink() or not base.is_dir():
            raise ValueError("缺少备份组成部分: " + prefix)
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError("备份成员含链接或路径越界")
            if path.is_file():
                files[path.relative_to(root).as_posix()] = fingerprint(path)
    if "config/.env" not in files:
        raise ValueError("缺少与密文配套的配置/加密密钥文件")
    return files


def backup(root, output, scope, *, quiesced=False):
    root, output = bound(root, scope), bound(output, scope)
    if not quiesced or output.exists() or output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("备份要求停写确认及新的独立输出目录")
    before = inventory(root)
    started = time.monotonic()
    output.mkdir(mode=0o700)
    try:
        with sqlite3.connect((root / "eic.db").as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(output / "eic.db") as target:
                source.backup(target)
                assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                if target.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("备份数据库存在外键缺陷")
        for prefix in ("uploads", "config"):
            shutil.copytree(root / prefix, output / prefix)
        if inventory(root) != before or inventory(output) != before:
            raise ValueError("停写期间文件发生变化，备份不得发布")
        files = {**before, "eic.db": fingerprint(output / "eic.db")}
        result = {"version": 1, "files": files, "seconds": time.monotonic() - started,
                  "quiesced": True, "scope": "synthetic_rehearsal", "secrets_logged": False}
        for path in output.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
        (output / "MANIFEST.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        (output / "MANIFEST.json").chmod(0o600)
        return result
    except BaseException:
        # 保留失败现场，不把部分备份标为完成。
        (output / "INCOMPLETE").write_text("备份失败，禁止恢复或发布", encoding="utf-8")
        raise


def restore(backup_root, output, scope):
    backup_root, output = bound(backup_root, scope), bound(output, scope)
    if output.exists() or output.is_relative_to(backup_root) or backup_root.is_relative_to(output):
        raise ValueError("仅允许恢复到新的独立目录；现有数据不覆盖")
    if (backup_root / "INCOMPLETE").exists():
        raise ValueError("备份不完整")
    if (backup_root / "MANIFEST.json").is_symlink():
        raise ValueError("备份清单含链接")
    record = json.loads((backup_root / "MANIFEST.json").read_text())
    allowed = inventory(backup_root)
    allowed["eic.db"] = fingerprint(backup_root / "eic.db")
    if record["version"] != 1 or record.get("quiesced") is not True or record["files"] != allowed:
        raise ValueError("备份成员或内容指纹不一致")
    started = time.monotonic()
    output.mkdir(mode=0o700)
    # 空上传目录也是恢复组成部分，不能仅按文件清单重建。
    for prefix in ("uploads", "config"):
        (output / prefix).mkdir(mode=0o700)
    for name, digest in allowed.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("备份成员路径无效")
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(backup_root / name, target)
        target.chmod(0o600)
        if fingerprint(target) != digest:
            raise ValueError("恢复内容指纹不一致")
    with sqlite3.connect(output / "eic.db") as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("恢复数据库完整性检查失败")
    return {"files": len(allowed), "seconds": time.monotonic() - started,
            "existing_data_overwritten": False, "activated": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("--scope-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quiesced", action="store_true")
    args = parser.parse_args()
    result = (backup(args.source, args.output, args.scope_root, quiesced=args.quiesced)
              if args.action == "backup" else restore(args.source, args.output, args.scope_root))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

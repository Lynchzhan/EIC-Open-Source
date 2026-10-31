"""仅为全新环境生成配置，不覆盖现有文件、不旋转旧密钥。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets

ROOT = Path(__file__).resolve().parents[1]


def initialize(root: Path, mode: str) -> Path:
    if mode not in {"backend", "deploy"}:
        raise ValueError("配置模式必须是 backend 或 deploy")
    folder = root / mode
    target = folder / ".env"
    if target.exists() or target.is_symlink():
        raise ValueError("配置文件已存在，请保留原密钥并手动检查；初始化不会覆盖")
    if mode == "backend" and (folder / "eic.db").exists():
        raise ValueError("已有数据库但缺少配置，请恢复原密钥；初始化已中止")
    example = folder / (".env.example" if mode == "backend" else "env.example")
    values = {
        "EIC_JWT_SECRET": secrets.token_hex(32),
        "EIC_SECRET_ENCRYPTION_KEY": secrets.token_hex(32),
        "EIC_BOOTSTRAP_ADMIN_PASSWORD": secrets.token_urlsafe(24) + "Aa1",
        "EIC_BOOTSTRAP_ENABLED": "true",
        "EIC_SEED_DEMO_DATA": "false",
    }
    if mode == "deploy":
        values.update({name: secrets.token_hex(24) for name in
                       ("MYSQL_PASSWORD", "MYSQL_ROOT_PASSWORD", "POSTGRES_PASSWORD")})
    lines = []
    for line in example.read_text(encoding="utf-8").splitlines():
        name, separator, _ = line.partition("=")
        lines.append(f"{name}={values[name]}" if separator and name in values else line)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["backend", "deploy"])
    args = parser.parse_args()
    try:
        target = initialize(ROOT, args.mode)
    except ValueError as exc:
        parser.exit(2, str(exc) + "\n")
    print(f"配置已生成：{target}")
    print("管理员用户名：admin；租户：1000。密码请查看配置中的 EIC_BOOTSTRAP_ADMIN_PASSWORD。")
    print("首次登录后将 EIC_BOOTSTRAP_ENABLED 改为 false。此文件不得提交到 Git。")


if __name__ == "__main__":
    main()

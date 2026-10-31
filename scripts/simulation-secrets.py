"""Linux/macOS 模拟密钥持久化；已有数据库时禁止静默替换密钥。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import secrets

NAMES = ("EIC_JWT_SECRET", "EIC_SECRET_ENCRYPTION_KEY")


def get_simulation_secrets(data_dir: Path) -> dict[str, str]:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "simulation.env"
    if path.is_symlink():
        raise ValueError("模拟密钥文件不得是符号链接")
    if not path.exists():
        if (data_dir / "eic-sim.db").exists():
            raise ValueError("已有模拟数据库但缺少 simulation.env；请恢复原密钥，禁止自动替换")
        body = "".join(f"{name}={secrets.token_hex(32)}\n" for name in NAMES)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or name in values or name not in NAMES:
            raise ValueError("模拟密钥配置格式错误或存在重复键")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,}", value) or value.startswith("replace-"):
            raise ValueError("模拟密钥无效；请恢复原配置，不自动生成替代值")
        values[name] = value
    if set(values) != set(NAMES):
        raise ValueError("模拟密钥缺失；请恢复原配置")
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    get_simulation_secrets(args.data_dir)
    print("模拟密钥已就绪；未向控制台输出密钥。")


if __name__ == "__main__":
    main()

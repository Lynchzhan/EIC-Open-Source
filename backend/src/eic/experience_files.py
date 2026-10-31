"""体验会话原文件隔离目录；只清理经过严格校验的生成路径。"""
import re
import shutil
import asyncio
import json
import sys
import os
import subprocess
from pathlib import Path
import logging

from eic.core.config import get_settings


def sessions_root():
    settings = get_settings()
    return settings.runtime_path(settings.experience_content_root).resolve() / "sessions"


def session_directory(key):
    if not re.fullmatch(r"session:[a-f0-9]{64}", key):
        raise ValueError("invalid experience session key")
    root = sessions_root()
    target = root / key.split(":", 1)[1]
    if root.is_symlink() or target.is_symlink() or target.resolve().parent != root.resolve():
        raise ValueError("invalid experience session path")
    return target


def attachment_path(session_key, attachment_id, suffix):
    if not re.fullmatch(r"attachment:[a-f0-9]{32}", attachment_id) or not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        raise ValueError("invalid experience attachment path")
    directory = session_directory(session_key)
    target = directory / (attachment_id.split(":", 1)[1] + suffix)
    if target.is_symlink() or target.resolve().parent != directory.resolve():
        raise ValueError("invalid experience attachment file")
    return target


def clear_session_files(key):
    directory = session_directory(key)
    if directory.exists():
        try:
            shutil.rmtree(directory)
        except OSError:
            # Windows 解析器短暂持有文件句柄时，不能回滚撤销资格；清理循环重试孤立目录。
            logging.getLogger(__name__).warning("体验会话文件暂时占用，等待清理循环重试")


def cleanup_orphans(active_keys):
    root = sessions_root()
    if not root.exists() or root.is_symlink():
        return
    for directory in root.iterdir():
        if re.fullmatch(r"[a-f0-9]{64}", directory.name) and "session:" + directory.name not in active_keys:
            clear_session_files("session:" + directory.name)


async def parse_attachment(path, name, timeout, check):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "eic.experience_parser", str(path), name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=env,
        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}),
    )
    reading = asyncio.create_task(process.communicate())
    try:
        async with asyncio.timeout(timeout):
            while not reading.done():
                await check()
                await asyncio.wait({reading}, timeout=0.5)
            stdout, _ = await reading
        if process.returncode != 0:
            raise ValueError("experience attachment parse failed")
        return json.loads(stdout)["text"]
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
        if not reading.done():
            reading.cancel()
        await asyncio.gather(reading, return_exceptions=True)

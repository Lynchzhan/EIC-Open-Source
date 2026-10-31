"""只读文件引用与预占盘点；只输出 dry-run，永远没有删除选项。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import time

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from eic.core.config import get_settings


def values(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            yield value
        else:
            if parsed == value:
                yield value
            else:
                yield from values(parsed)
    elif isinstance(value, dict):
        for item in value.values():
            yield from values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from values(item)


async def audit(url, root, min_age_seconds=None):
    root = Path(root).resolve(strict=True)
    engine = create_async_engine(url)
    references = set()
    reservations = []
    try:
        async with engine.connect() as connection:
            if connection.dialect.name == "sqlite":
                await connection.execute(text("PRAGMA query_only = ON"))
            elif connection.dialect.name == "postgresql":
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            tables = await connection.run_sync(lambda c: inspect(c).get_table_names())
            for table in tables:
                quoted = connection.dialect.identifier_preparer.quote(table)
                async for row in await connection.stream(text("SELECT * FROM " + quoted)):
                    for value in row:
                        for item in values(value):
                            if "\x00" in item or len(item) > 8192:
                                continue
                            try:
                                path = Path(item)
                                if path.is_absolute():
                                    path = path.resolve()
                                    if root in path.parents:
                                        references.add(path)
                            except (OSError, ValueError):
                                continue
                    if table == "upload_reservations":
                        data = row._mapping
                        reservations.append({"token": data["token"], "tenant_id": str(data["tenant_id"]),
                                             "reserved_bytes": data["reserved_bytes"],
                                             "file_count": data["file_count"],
                                             "created_at": str(data["created_at"])})
            await connection.rollback()
    finally:
        await engine.dispose()
    schema_known = "upload_reservations" in tables
    entries = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file():
            entries.append({"name": path.name, "state": "ignored_special", "eligible": False})
            continue
        protected = path.resolve() in references
        age = max(0, time.time() - path.stat().st_mtime)
        # 有未决预占时，发布后尚未提交的随机文件也可能暂时没有引用。
        blocked = bool(reservations) or not schema_known
        eligible = not protected and not blocked and min_age_seconds is not None and age >= min_age_seconds
        entries.append({"name": path.name, "bytes": path.stat().st_size, "age_seconds": round(age, 3),
                        "state": "referenced" if protected else "unreferenced",
                        "blocked_by_unresolved_reservations": blocked, "eligible": eligible})
    return {"read_only": True, "dry_run": True, "deletions": 0, "reservation_releases": 0,
            "at": datetime.now(UTC).isoformat(), "root": str(root), "min_age_seconds": min_age_seconds,
            "retention_policy_approved": False, "reservation_schema_known": schema_known,
            "reservations": reservations, "files": entries,
            "warning": "Eligibility is diagnostic only; no cleanup or lease expiry is performed."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-age-seconds", type=int)
    args = parser.parse_args()
    if args.min_age_seconds is not None and args.min_age_seconds < 0:
        parser.error("--min-age-seconds must be nonnegative")
    settings = get_settings()
    print(json.dumps(asyncio.run(audit(
        settings.database_url, settings.runtime_path(settings.upload_root), args.min_age_seconds,
    )), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

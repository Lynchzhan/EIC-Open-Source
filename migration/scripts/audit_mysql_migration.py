"""只读核对旧库与EIC实体/关系；不可用结果永远不等同于匹配。"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from migrate_mysql import SOURCE_TABLES, read_source, redacted_payload, safe_url
from migration_integrity import read_only_engine, verify_integrity
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def audit(source_url, target_url=None):
    report = {"generated_at": datetime.now(UTC).isoformat(), "read_only": True,
              "source_endpoint": safe_url(source_url), "status": "failed"}
    source_engine = target_engine = None
    try:
        source_engine = await read_only_engine(source_url)
        tables, errors = await read_source(source_engine, allow_missing=True)
        report["source"] = {table: len(tables[table]) if table in tables else None for table in SOURCE_TABLES}
        report["source_errors"] = errors
        if not target_url:
            report["status"] = "partial" if errors else "success"
            report["scope"] = "source inventory only; no target integrity verified"
            return report
        report["target_endpoint"] = safe_url(target_url)
        target_engine = await read_only_engine(target_url)
        maker = async_sessionmaker(target_engine, expire_on_commit=False, class_=AsyncSession)
        async with maker() as session:
            if target_engine.dialect.name in {"postgresql", "mysql"}:
                await session.execute(text("SET TRANSACTION READ ONLY"))
            integrity = await verify_integrity(session, tables, errors, redacted_payload)
            await session.rollback()
        report["integrity"] = integrity
        report["target"] = {table: integrity["tables"].get(table, {}).get("target_verified") for table in SOURCE_TABLES}
        report["match"] = {
            table: table in tables and table not in errors and
            integrity["tables"][table]["source"] == integrity["tables"][table]["target_verified"] ==
            integrity["tables"][table]["archived"] and integrity["tables"][table]["duplicates"] == 0 and
            not any(item["table"] == table for item in integrity["issues"] + integrity["fatal_errors"])
            for table in SOURCE_TABLES
        }
        report["status"] = "failed" if integrity["fatal_errors"] else "success" if integrity["complete"] else "partial"
        return report
    except Exception as exc:
        report.update(error=type(exc).__name__, match={table: False for table in SOURCE_TABLES})
        return report
    finally:
        if source_engine is not None:
            await source_engine.dispose()
        if target_engine is not None:
            await target_engine.dispose()


async def main(source_url, target_url, output):
    report = await audit(source_url, target_url)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(output))
    return report


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--target-url")
    parser.add_argument("--output", type=Path, default=SCRIPT_DIR.parent / "reports/mysql_migration_audit.json")
    args = parser.parse_args()
    report = asyncio.run(main(args.source_url, args.target_url, args.output))
    raise SystemExit({"success": 0, "partial": 3, "failed": 2}[report["status"]])


if __name__ == "__main__":
    cli()

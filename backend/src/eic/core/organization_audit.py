"""只读盘点部门异常和未归属的历史共享已读，不执行回填或修复。"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from eic.core.config import get_settings
from eic.core.message_reads import read_fields
from eic.core.organization import cycle_members


async def audit() -> dict:
    url = get_settings().database_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        path = Path(parsed.database).resolve()
        if not path.is_file():
            raise RuntimeError("Read-only audit requires an existing database")
        url = "sqlite+aiosqlite:///file:" + path.as_posix() + "?mode=ro&uri=true"
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            if parsed.get_backend_name() == "sqlite":
                await connection.exec_driver_sql("PRAGMA query_only=ON")
            else:
                await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            departments = (await connection.execute(text(
                "SELECT tenant_id,id,parent_id FROM departments WHERE active=true ORDER BY tenant_id,id"
            ))).all()
            messages = (await connection.execute(text(
                "SELECT tenant_id,id,data FROM resources WHERE resource_type='messages' AND is_deleted=false ORDER BY tenant_id,id"
            ))).all()
            tenants = {}
            for tenant_id, department_id, parent_id in departments:
                item = tenants.setdefault(tenant_id, {"parents": {}, "legacy": []})
                item["parents"][department_id] = parent_id
            for tenant_id, message_id, data in messages:
                item = tenants.setdefault(tenant_id, {"parents": {}, "legacy": []})
                data = json.loads(data) if isinstance(data, str) else data or {}
                if read_fields(data)["readState"] == "unknown":
                    item["legacy"].append(str(message_id))
            await connection.rollback()
    finally:
        await engine.dispose()
    result = []
    for tenant_id, item in sorted(tenants.items()):
        parents = item["parents"]
        result.append({
            "tenantId": str(tenant_id), "departmentCount": len(parents),
            "cycleIds": [str(value) for value in sorted(cycle_members(parents))],
            "orphanIds": [str(value) for value, parent in parents.items() if parent is not None and parent not in parents],
            "unattributedLegacyReadIds": item["legacy"],
        })
    payload = json.dumps({"departments": [tuple(row) for row in departments],
                          "messages": [tuple(row) for row in messages]}, default=str, sort_keys=True)
    return {
        "read_only": True, "business_rows_changed": 0, "personal_receipts_backfilled": 0,
        "recipient_inference_performed": False, "tenants": result,
        "snapshot_sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = json.dumps(asyncio.run(audit()), ensure_ascii=False, indent=2)
    if args.output:
        if args.output.exists():
            raise RuntimeError("Audit output already exists")
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)


if __name__ == "__main__":
    main()

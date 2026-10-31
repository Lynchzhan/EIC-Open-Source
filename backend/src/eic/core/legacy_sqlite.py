"""只读快照旧版 cloud-lite SQLite，生成独立版本化副本；不替换源库。"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from eic.core.config import BACKEND_ROOT


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def signature(connection: sqlite3.Connection) -> dict:
    tables = [r[0] for r in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )]
    result = {}
    for name in tables:
        indexes = []
        for row in connection.execute("PRAGMA index_list(" + quote(name) + ")"):
            columns = [r[2] for r in connection.execute("PRAGMA index_info(" + quote(row[1]) + ")")]
            indexes.append((row[2], columns, row[4]))
        result[name] = {
            "columns": connection.execute("PRAGMA table_info(" + quote(name) + ")").fetchall(),
            "foreign_keys": connection.execute("PRAGMA foreign_key_list(" + quote(name) + ")").fetchall(),
            "indexes": sorted(indexes, key=repr),
        }
    return result


def rows_digest(rows) -> dict:
    encoded = sorted(json.dumps(tuple(row), ensure_ascii=True,
                                default=lambda value: {"bytes": value.hex()}) for row in rows)
    return {"count": len(encoded), "sha256": hashlib.sha256("\n".join(encoded).encode()).hexdigest()}


def row_digest(connection: sqlite3.Connection, table: str, columns: list[str]) -> dict:
    return rows_digest(connection.execute("SELECT " + ",".join(map(quote, columns)) + " FROM " + quote(table)))


def orphan_ingestion_rows(connection: sqlite3.Connection) -> list[tuple]:
    """只选父文档缺失的日志，不扩大归档范围到其他外键或其他业务表。"""
    return connection.execute(
        "SELECT * FROM knowledge_ingestion_runs WHERE document_id NOT IN (SELECT id FROM knowledge_documents)"
    ).fetchall()


def expected_signature() -> dict:
    sql = (BACKEND_ROOT / "migrations/legacy_cloud_lite.sql").read_text(encoding="utf-8")
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    with sqlite3.connect(":memory:") as connection:
        for statement in sorted(statements, key=lambda value: not value.startswith("CREATE TABLE")):
            connection.execute(statement)
        return signature(connection)


def prepare_copy(source: Path, destination: Path, *, orphan_ingestion_sha256: str | None = None) -> dict:
    """成功仅证明一致快照；切换前仍须停止旧 writer 并保护快照后的写入。"""
    if source.is_symlink() or not source.is_file():
        raise ValueError("源库必须是已确认的普通文件")
    source = source.resolve()
    destination = destination.absolute()
    if source == destination.resolve() or destination.exists() or destination.is_symlink():
        raise ValueError("迁移目标必须是新的独立文件；保留原库和现有副本")
    if not destination.parent.is_dir():
        raise ValueError("迁移目标父目录不存在")
    work = Path(tempfile.mkdtemp(prefix=".eic-legacy-", dir=destination.parent))
    work.chmod(0o700)
    snapshot = work / "source.db"
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as live:
        with sqlite3.connect(snapshot) as frozen:
            live.backup(frozen)
    snapshot.chmod(0o600)
    with sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True) as old:
        if signature(old) != expected_signature():
            raise ValueError("旧库 schema 与已审查的 28 表版本不同；需要定向补审")
        if old.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("旧库完整性检查失败；保留快照供复核")
        violations = old.execute("PRAGMA foreign_key_check").fetchall()
        archived = []
        if violations:
            if orphan_ingestion_sha256 is None:
                raise ValueError("旧库存在外键异常，迁移副本未发布；异常数量=" + str(len(violations)))
            if any(table != "knowledge_ingestion_runs" or parent != "knowledge_documents" or fk != 0
                   for table, _, parent, fk in violations):
                raise ValueError("存在归档范围以外的外键异常；迁移副本未发布")
            archived = orphan_ingestion_rows(old)
            if len(archived) != len(violations) or not hmac.compare_digest(
                    rows_digest(archived)["sha256"], orphan_ingestion_sha256):
                raise ValueError("孤儿日志摘要与审核值不符；迁移副本未发布")
        elif orphan_ingestion_sha256 is not None:
            raise ValueError("未发现预期孤儿日志；禁止静默忽略审核值")
        target = work / "candidate.db"
        env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + target.as_posix()}
        argv = [sys.executable, "-B", "-m", "eic.core.schema", "upgrade"]
        migrated = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=180)
        if migrated.returncode:
            raise RuntimeError("独立目标 schema 创建失败；退出码=" + str(migrated.returncode))
        receipts = {}
        with sqlite3.connect(target) as new:
            new.execute("PRAGMA foreign_keys=ON")
            new.execute("BEGIN")
            new.execute("PRAGMA defer_foreign_keys=ON")
            if archived:
                # 无外键的历史区保留全部原列；主业务表不保留无效的文档关联。
                new.execute("CREATE TABLE legacy_orphan_ingestion_archive AS "
                            "SELECT * FROM knowledge_ingestion_runs WHERE 0")
                old_columns = [r[1] for r in signature(old)["knowledge_ingestion_runs"]["columns"]]
                new.executemany("INSERT INTO legacy_orphan_ingestion_archive("
                                + ",".join(map(quote, old_columns)) + ") VALUES("
                                + ",".join("?" for _ in old_columns) + ")", archived)
                if rows_digest(archived) != row_digest(new, "legacy_orphan_ingestion_archive", old_columns):
                    raise RuntimeError("孤儿日志完整归档校验失败")
                new.execute("CREATE TABLE legacy_orphan_ingestion_manifest("
                            "source_snapshot_sha256 TEXT NOT NULL, row_sha256 TEXT NOT NULL, "
                            "row_count INTEGER NOT NULL, reason TEXT NOT NULL)")
                new.execute("INSERT INTO legacy_orphan_ingestion_manifest VALUES(?,?,?,?)",
                            (hashlib.sha256(snapshot.read_bytes()).hexdigest(), orphan_ingestion_sha256,
                             len(archived), "missing_parent_document; source_unchanged; no_provider_replay"))
            high = 2_000_000_000_000_000 - 1
            for table, spec in signature(old).items():
                columns = [r[1] for r in spec["columns"]]
                target_columns = {r[1] for r in new.execute("PRAGMA table_info(" + quote(table) + ")")}
                if not set(columns).issubset(target_columns):
                    raise ValueError("目标缺少旧列；禁止丢弃字段：" + table)
                names = ",".join(map(quote, columns))
                condition = (" WHERE document_id IN (SELECT id FROM knowledge_documents)"
                             if archived and table == "knowledge_ingestion_runs" else "")
                rows = old.execute("SELECT " + names + " FROM " + quote(table) + condition)
                new.executemany("INSERT INTO " + quote(table) + "(" + names + ") VALUES("
                                + ",".join("?" for _ in columns) + ")", rows)
                receipts[table] = row_digest(old, table, columns)
                reconstructed = new.execute("SELECT " + names + " FROM " + quote(table)
                                            + (" UNION ALL SELECT " + names + " FROM legacy_orphan_ingestion_archive"
                                               if archived and table == "knowledge_ingestion_runs" else ""))
                if receipts[table] != rows_digest(reconstructed):
                    raise RuntimeError("旧列逐行摘要不一致：" + table)
                if "id" in columns:
                    values = old.execute("SELECT MAX(id) FROM " + quote(table)).fetchone()[0]
                    if isinstance(values, int):
                        high = max(high, values)
            if high >= 2**63 - 1:
                raise ValueError("历史 ID 已用尽 64 位分配空间")
            new.execute("UPDATE id_allocator SET value=? WHERE name='global'", (high,))
            # 旧 generating 只纳入过期租约恢复，绝不重发 provider。
            messages = new.execute(
                "SELECT id,tenant_id,request_id FROM chat_messages WHERE status='generating'"
            ).fetchall()
            for mid, tid, request_id in messages:
                if not request_id:
                    raise ValueError("遗留 generating 缺少 request_id；需要定向补审")
                new.execute("INSERT INTO generation_leases VALUES(?,?,?,?,?,?)",
                            (mid, tid, request_id, uuid.uuid4().hex,
                             "1970-01-01 00:00:00.000000", "active"))
            # 数据先导入再执行旧索引的保守标记，不将旧切块认作 embedding 成功。
            new.execute("UPDATE knowledge_documents SET parse_status='legacy', "
                        "embedding_status='unverified',index_status='legacy' "
                        "WHERE active_index_version=0 AND parse_status='pending'")
            if new.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("目标外键检查失败；迁移副本未发布")
            new.commit()
            revision = new.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        checked = subprocess.run([sys.executable, "-B", "-m", "eic.core.schema", "check"],
                                 env=env, capture_output=True, text=True, timeout=90)
        if checked.returncode:
            raise RuntimeError("独立目标 schema check 失败")
        target.chmod(0o600)
        # hard link 不覆盖竞态期间出现的已有目标，且只有验证通过的完整库可发布。
        os.link(target, destination)
        return {
            "passed": True, "source": str(source), "destination": str(destination),
            "source_snapshot": str(snapshot),
            "source_snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            "destination_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "old_table_receipts": receipts, "old_column_values_preserved": True,
            "orphan_ingestion_archive": {
                "count": len(archived), "sha256": orphan_ingestion_sha256,
                "table": "legacy_orphan_ingestion_archive" if archived else None,
                "all_original_columns_preserved": True, "active_rows_plus_archive_match_source": True,
            },
            "schema_head": revision, "allocator_high": high, "expired_generation_leases": len(messages),
            "provider_requests_replayed": 0, "original_database_modified": False,
            "snapshot_only": True, "cutover_requires_writer_freeze_and_new_write_protection": True,
            "migration": {"argv": argv, "exit_code": migrated.returncode,
                          "stdout": migrated.stdout, "stderr": migrated.stderr},
            "check": {"exit_code": checked.returncode, "stdout": checked.stdout, "stderr": checked.stderr},
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--orphan-ingestion-sha256",
                        help="显式审核的孤儿日志逐行摘要；仅归档父文档缺失的日志，默认仍拒绝异常")
    args = parser.parse_args()
    # 证据文件也不覆盖；失败报告不含业务值或密钥。
    with args.report.open("x", encoding="utf-8") as report:
        try:
            record = prepare_copy(args.source, args.destination,
                                  orphan_ingestion_sha256=args.orphan_ingestion_sha256)
        except (ValueError, RuntimeError, sqlite3.Error, OSError, subprocess.TimeoutExpired) as error:
            json.dump({"passed": False, "error_type": type(error).__name__, "message": str(error)},
                      report, ensure_ascii=False, indent=2)
            raise SystemExit("迁移副本未发布：" + str(error)) from None
        json.dump(record, report, ensure_ascii=False, indent=2)
    print("LEGACY_COPY_VERIFIED 原库保留；切换和观察验收尚未执行")


if __name__ == "__main__":
    main()

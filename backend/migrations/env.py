from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from eic.core.config import get_settings
from eic.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def prepare_version_table(connection: Connection) -> None:
    # 保留所有历史 revision 字面值；不要改名或 stamp 跳过迁移。
    inspector = sa.inspect(connection)
    if not inspector.has_table("alembic_version"):
        metadata = sa.MetaData()
        sa.Table(
            "alembic_version", metadata,
            sa.Column("version_num", sa.String(128), nullable=False),
            sa.PrimaryKeyConstraint("version_num", name="alembic_version_pkc"),
        ).create(connection)
    else:
        columns = {column["name"]: column for column in inspector.get_columns("alembic_version")}
        column = columns.get("version_num")
        if column is None or not isinstance(column["type"], sa.String):
            raise RuntimeError("Incompatible Alembic version table; manual review required")
        # SQLite 不强制 VARCHAR 长度；避免为版本记录重建已有 SQLite 表。
        if connection.dialect.name != "sqlite" and column["type"].length and column["type"].length < 128:
            Operations(MigrationContext.configure(connection)).alter_column(
                "alembic_version", "version_num", type_=sa.String(128),
                existing_type=column["type"], existing_nullable=column["nullable"],
            )
    # 准备事务独立提交，保留 Alembic 对非事务 DDL 的逐 revision 回执语义。
    connection.commit()


def do_run_migrations(connection: Connection) -> None:
    inspector = sa.inspect(connection)
    existing = set(inspector.get_table_names()) - {"alembic_version"}
    heads = MigrationContext.configure(connection).get_current_heads()
    if existing and not heads:
        raise RuntimeError(
            "未版本化旧库禁止原地升级；请先使用 python -m eic.core.legacy_sqlite "
            "生成并验证独立迁移副本，保留原库和新写入"
        )
    prepare_version_table(connection)
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = async_engine_from_config(configuration, prefix="sqlalchemy.", pool_pre_ping=True)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

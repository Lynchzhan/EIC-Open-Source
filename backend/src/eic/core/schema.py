"""Read-only startup contract; versioned upgrades run as a separate release step."""
from __future__ import annotations

import argparse
import asyncio

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from eic.core.config import BACKEND_ROOT, get_settings
from eic.models import Base


def assert_schema_ready(connection) -> None:
    expected = set(ScriptDirectory(str(BACKEND_ROOT / "migrations")).get_heads())
    actual = set(MigrationContext.configure(connection).get_current_heads())
    if actual != expected:
        raise RuntimeError("Database revision is not ready; run the isolated release migration step")
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            raise RuntimeError("Database schema is missing table: " + table.name)
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing = set(table.columns.keys()) - columns
        if missing:
            raise RuntimeError("Database schema is missing columns: " + table.name + "." + ",".join(sorted(missing)))


async def require_schema(engine) -> None:
    async with engine.connect() as connection:
        await connection.run_sync(assert_schema_ready)


async def check() -> None:
    engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    try:
        await require_schema(engine)
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "upgrade"])
    arguments = parser.parse_args()
    if arguments.action == "upgrade":
        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
        config.set_main_option("prepend_sys_path", str(BACKEND_ROOT / "src"))
        config.set_main_option("path_separator", "os")
        command.upgrade(config, "head")
    asyncio.run(check())
    print("SCHEMA_READY action=" + arguments.action)


if __name__ == "__main__":
    main()

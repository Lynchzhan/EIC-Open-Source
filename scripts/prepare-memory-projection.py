"""显式准备独立记忆投影；不回填、不调用模型。"""
import argparse
import asyncio
from pathlib import Path


async def main(dimensions):
    from eic.vector.memory_store import prepare
    sql = Path(__file__).resolve().parents[1] / "deploy/postgres-init/002-memory.sql"
    await prepare(sql.read_text(encoding="utf-8"), dimensions)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dimensions", type=int, required=True)
    args = parser.parse_args()
    asyncio.run(main(args.dimensions))

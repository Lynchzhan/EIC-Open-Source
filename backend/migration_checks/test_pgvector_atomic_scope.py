"""离线SQLite执行同一冲突谓词；不等价真实PostgreSQL/pgvector验证。"""
import asyncio
from dataclasses import replace
import sqlite3

import pytest

from eic.vector.pgvector_store import ChunkRecord, PgVectorStore


class SQLiteProjection:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.calls = []
        self.db.execute('CREATE TABLE rag_vectors (chunk_id TEXT PRIMARY KEY, tenant_id INTEGER, knowledge_base_id INTEGER, document_id INTEGER, content TEXT, embedding TEXT, metadata TEXT)')

    def sql(self, query, args):
        self.calls.append((query, args))
        return self.db.execute(query.replace('::vector', '').replace('::jsonb', ''), args)

    async def execute(self, query, *args):
        self.sql(query, args)

    async def fetchval(self, query, *args):
        row = self.sql(query, args).fetchone()
        return row[0] if row else None

    def row(self):
        return self.db.execute('SELECT * FROM rag_vectors').fetchone()


def original():
    return ChunkRecord('same-id', 11, 22, 33, 'original', [1.0, 2.0], {'parent': 'original-parent'})


@pytest.mark.parametrize('change', [{'tenant_id': 99}, {'knowledge_base_id': 99}, {'document_id': 99}])
def test_collision_preserves_entire_existing_row(change):
    async def scenario():
        conn = SQLiteProjection()
        try:
            store = PgVectorStore(2)
            await store.upsert(conn, original())
            before = conn.row()
            changed = replace(original(), **change, content='hostile', vector=[8.0, 9.0], metadata={'parent': 'other-parent'})
            with pytest.raises(ValueError, match='pgvector_chunk_scope_conflict') as error:
                await store.upsert(conn, changed)
            from eic.vector.pgvector_store import PgVectorScopeConflict
            assert isinstance(error.value, PgVectorScopeConflict)
            assert conn.row() == before
            assert len(conn.calls) == 2  # 每次调用仅一条SQL，没有先查后写。
        finally:
            conn.db.close()
    asyncio.run(scenario())


def test_same_scope_updates_content_vector_metadata_and_keeps_owner():
    async def scenario():
        conn = SQLiteProjection()
        try:
            store = PgVectorStore(2)
            await store.upsert(conn, original())
            await store.upsert(conn, replace(original(), content='updated', vector=[3.0, 4.0], metadata={'parent': 'same-document-parent'}))
            row = conn.row()
            assert row[:5] == ('same-id', 11, 22, 33, 'updated')
            assert row[5:] == ('[3.0, 4.0]', '{"parent": "same-document-parent"}')
            assert len(conn.calls) == 2
        finally:
            conn.db.close()
    asyncio.run(scenario())


def test_dimension_failure_prevents_dispatch():
    async def scenario():
        conn = SQLiteProjection()
        try:
            with pytest.raises(ValueError, match='dimension mismatch'):
                await PgVectorStore(3).upsert(conn, original())
            assert not conn.calls
        finally:
            conn.db.close()
    asyncio.run(scenario())


def test_sql_contract_is_single_guarded_upsert():
    class RecordingConnection:
        async def fetchval(self, query, *args):
            self.query, self.args = query, args
            return 1
    async def scenario():
        conn = RecordingConnection()
        await PgVectorStore(2).upsert(conn, original())
        assert 'ON CONFLICT (chunk_id)' in conn.query
        for field in ('tenant_id', 'knowledge_base_id', 'document_id'):
            assert f'rag_vectors.{field} = EXCLUDED.{field}' in conn.query
        assert 'RETURNING 1' in conn.query
        assert conn.args[:4] == ('same-id', 11, 22, 33)
    asyncio.run(scenario())


def test_outer_batch_can_rollback_on_scope_conflict():
    async def scenario():
        from eic.vector.pgvector_store import PgVectorScopeConflict
        conn = SQLiteProjection()
        try:
            store = PgVectorStore(2)
            await store.upsert(conn, original())
            conn.db.commit()
            before = conn.row()
            conn.db.execute('BEGIN')
            try:
                await store.upsert(conn, replace(original(), chunk_id='new-batch-chunk'))
                await store.upsert(conn, replace(original(), tenant_id=99))
            except PgVectorScopeConflict:
                conn.db.rollback()
            else:
                pytest.fail('collision failed to abort batch')
            assert conn.db.execute('SELECT COUNT(*) FROM rag_vectors').fetchone()[0] == 1
            assert conn.row() == before
        finally:
            conn.db.close()
    asyncio.run(scenario())


def test_database_errors_propagate_unchanged():
    failure = RuntimeError('synthetic DB failure')
    class BrokenConnection:
        async def fetchval(self, query, *args):
            raise failure
    async def scenario():
        with pytest.raises(RuntimeError) as error:
            await PgVectorStore(2).upsert(BrokenConnection(), original())
        assert error.value is failure
    asyncio.run(scenario())

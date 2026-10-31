"""实际数据库/HTTP 故障注入；生产策略不在本测试中批准。"""
import asyncio
from io import BytesIO
import itertools
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from eic import main
from eic.core.database import SessionLocal, engine
from auth_fixture import signed_headers
from eic.core.uploads import LIMIT_FIELDS, UploadBodyLimit, save_uploads
from eic.core.upload_audit import audit
from eic.models import Resource, Tenant, UploadReservation, User

SERIAL = itertools.count(900000)


@pytest.fixture
def fixture(monkeypatch, tmp_path):
    tenant = next(SERIAL)
    user = tenant + 100000

    async def seed():
        async with SessionLocal() as session:
            session.add(Tenant(id=tenant, code=tenant, name="Upload fixture"))
            await session.flush()
            session.add(User(id=user, tenant_id=tenant, username="upload-fixture", password_hash="not-a-login",
                             is_super_admin=True))
            await session.commit()
        await engine.dispose()

    asyncio.run(seed())
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setattr(main, "UPLOAD_ROOT", root)
    monkeypatch.setattr(main.settings, "upload_root", str(root))
    for name in LIMIT_FIELDS:
        monkeypatch.setattr(main.settings, "upload_" + name, None)
    headers = signed_headers(user, tenant, "upload-fixture", "not-a-login")
    principal = SimpleNamespace(tenant_id=tenant, user_id=user)
    with TestClient(main.app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, headers=headers, root=root, principal=principal)


async def rows(principal):
    async with SessionLocal() as session:
        files = list(await session.scalars(select(Resource).where(
            Resource.tenant_id == principal.tenant_id, Resource.resource_type == "files",
        )))
        reservations = list(await session.scalars(select(UploadReservation).where(
            UploadReservation.tenant_id == principal.tenant_id,
        )))
        return files, reservations


def upload(fixture, data=b"fixture", filename="fixture.txt"):
    return fixture.client.post("/api/File/UploadFile", headers=fixture.headers,
                               files={"file": (filename, data, "text/plain")})


def empty(fixture):
    files, reservations = asyncio.run(rows(fixture.principal))
    assert files == [] and reservations == [] and list(fixture.root.iterdir()) == []


def test_normal_private_upload_uses_independent_path_and_no_reservation(fixture):
    result = upload(fixture)
    assert result.status_code == 200 and "path" not in result.json()
    files, reservations = asyncio.run(rows(fixture.principal))
    assert len(files) == 1 and not reservations and files[0].owner_id == fixture.principal.user_id
    path = Path(files[0].data["path"])
    assert path.name != str(files[0].id) + ".txt" and path.read_bytes() == b"fixture"
    assert files[0].data["accessMode"] == "private"
    response = fixture.client.get("/api/File/GetFile", headers=fixture.headers,
                                  params={"fileid": result.json()["id"]})
    assert response.status_code == 200 and response.content == b"fixture"


@pytest.mark.parametrize("batch", [False, True])
def test_metadata_commit_failure_compensates_every_file(fixture, monkeypatch, batch):
    original = AsyncSession.commit

    async def fail(session):
        if any(isinstance(row, Resource) and row.resource_type == "files" for row in session.new):
            raise RuntimeError("synthetic-file-metadata-commit-failure")
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", fail)
    if batch:
        result = fixture.client.post("/api/File/BatchUploadFile", headers=fixture.headers,
                                     files=[("files", ("one.txt", b"one")), ("files", ("two.txt", b"two"))])
    else:
        result = upload(fixture)
    assert result.status_code == 500
    empty(fixture)


def test_exception_after_successful_commit_does_not_delete_referenced_file(fixture, monkeypatch):
    original = AsyncSession.commit

    async def uncertain(session):
        metadata = any(isinstance(row, Resource) and row.resource_type == "files" for row in session.new)
        await original(session)
        if metadata:
            raise RuntimeError("synthetic-response-lost-after-real-commit")

    monkeypatch.setattr(AsyncSession, "commit", uncertain)
    assert upload(fixture).status_code == 500
    files, reservations = asyncio.run(rows(fixture.principal))
    assert len(files) == 1 and not reservations
    assert Path(files[0].data["path"]).read_bytes() == b"fixture"


@pytest.mark.parametrize("error", [OSError("synthetic-read-interruption"), asyncio.CancelledError()])
def test_read_interruption_and_cancellation_compensate(fixture, error):
    class Broken:
        size = 7
        filename = "interrupted.txt"
        content_type = "text/plain"

        async def read(self, size):
            raise error

    async def execute():
        async with SessionLocal() as session:
            await save_uploads([Broken()], fixture.principal, session, fixture.root, main.settings,
                               SessionLocal, main.lock_tenant_writes)

    with pytest.raises(type(error)):
        asyncio.run(execute())
    empty(fixture)


def test_all_application_reads_are_chunk_bounded(fixture):
    calls = []

    class Tracked(UploadFile):
        async def read(self, size=-1):
            calls.append(size)
            assert 0 < size <= main.settings.upload_chunk_bytes
            return await super().read(size)

    async def execute():
        file = Tracked(filename="chunk.txt", file=BytesIO(b"x" * 200000), size=200000)
        async with SessionLocal() as session:
            return await save_uploads([file], fixture.principal, session, fixture.root, main.settings,
                                      SessionLocal, main.lock_tenant_writes)

    assert asyncio.run(execute())[0]["size"] == 200000 and len(calls) >= 4


@pytest.mark.parametrize("field,value,reason", [
    ("max_file_bytes", 3, "file_bytes"), ("max_request_bytes", 3, "request_bytes"),
    ("tenant_max_bytes", 3, "tenant_bytes"),
])
def test_configured_byte_limits_reject_before_storage(fixture, monkeypatch, field, value, reason):
    monkeypatch.setattr(main.settings, "upload_" + field, value)
    result = upload(fixture)
    assert result.status_code == 413 and result.json()["detail"]["reason"] == reason
    empty(fixture)


def test_batch_count_and_total_limits_have_atomic_failure(fixture, monkeypatch):
    files = [("files", ("one.txt", b"one")), ("files", ("two.txt", b"two"))]
    monkeypatch.setattr(main.settings, "upload_max_files", 1)
    result = fixture.client.post("/api/File/BatchUploadFile", headers=fixture.headers, files=files)
    assert result.status_code == 413
    empty(fixture)
    monkeypatch.setattr(main.settings, "upload_max_files", 2)
    monkeypatch.setattr(main.settings, "upload_max_request_bytes", 5)
    assert fixture.client.post("/api/File/BatchUploadFile", headers=fixture.headers, files=files).status_code == 413
    empty(fixture)


def test_underreported_actual_size_rejects_and_cleans_temp(fixture):
    async def execute():
        file = UploadFile(filename="false-size.txt", file=BytesIO(b"too-large"), size=1)
        async with SessionLocal() as session:
            await save_uploads([file], fixture.principal, session, fixture.root, main.settings,
                               SessionLocal, main.lock_tenant_writes)
    with pytest.raises(HTTPException) as error:
        asyncio.run(execute())
    assert error.value.status_code == 413
    empty(fixture)


def test_unknown_size_with_quota_fails_closed(fixture, monkeypatch):
    monkeypatch.setattr(main.settings, "upload_tenant_max_bytes", 10)

    async def execute():
        async with SessionLocal() as session:
            await save_uploads([UploadFile(filename="unknown", file=BytesIO(b"x"))], fixture.principal,
                               session, fixture.root, main.settings, SessionLocal, main.lock_tenant_writes)
    with pytest.raises(HTTPException) as error:
        asyncio.run(execute())
    assert error.value.status_code == 411
    empty(fixture)


def test_soft_deleted_retained_file_still_consumes_quota(fixture, monkeypatch):
    first = upload(fixture)
    assert first.status_code == 200

    async def deleted():
        async with SessionLocal() as session:
            item = await session.get(Resource, int(first.json()["id"]))
            item.is_deleted = True
            await session.commit()
    asyncio.run(deleted())
    monkeypatch.setattr(main.settings, "upload_tenant_max_files", 1)
    assert upload(fixture).status_code == 413
    assert len(list(fixture.root.iterdir())) == 1


def test_body_limit_rejects_before_metadata(fixture, monkeypatch):
    monkeypatch.setattr(main.settings, "upload_max_body_bytes", 8)
    assert upload(fixture).status_code == 413
    empty(fixture)


def test_chunked_body_counts_without_content_length():
    from fastapi import FastAPI
    from starlette.requests import Request
    app = FastAPI()
    app.add_middleware(UploadBodyLimit, settings=SimpleNamespace(upload_max_body_bytes=5))

    @app.post("/api/File/UploadFile")
    async def endpoint(request: Request):
        return len(await request.body())

    async def execute():
        sent = []
        parts = iter([{"type": "http.request", "body": b"aaa", "more_body": True},
                      {"type": "http.request", "body": b"bbb", "more_body": False}])
        async def receive():
            return next(parts)
        async def send(message):
            sent.append(message)
        await app({"type": "http", "method": "POST", "path": "/api/File/UploadFile",
                   "headers": [], "query_string": b"", "scheme": "http", "server": ("test", 80),
                   "client": ("test", 1), "root_path": "", "http_version": "1.1"}, receive, send)
        assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 413
    asyncio.run(execute())


def test_chunked_multipart_limit_closes_parser_temporary_files(fixture, monkeypatch):
    import starlette.formparsers as parsers
    original = parsers.SpooledTemporaryFile
    handles = []
    def tracked(*args, **kwargs):
        handle = original(*args, **kwargs)
        handles.append(handle)
        return handle
    monkeypatch.setattr(parsers, "SpooledTemporaryFile", tracked)
    monkeypatch.setattr(main.settings, "upload_max_body_bytes", 3000)
    first = (b'--upload-boundary\r\nContent-Disposition: form-data; name="file"; filename="chunk.txt"\r\n'
             b"Content-Type: text/plain\r\n\r\n" + b"x" * 2000)
    second = b"x" * 2000 + b"\r\n--upload-boundary--\r\n"

    async def execute():
        sent = []
        parts = iter([{"type": "http.request", "body": first, "more_body": True},
                      {"type": "http.request", "body": second, "more_body": False}])
        async def receive():
            return next(parts)
        async def send(message):
            sent.append(message)
        await main.app({"type": "http", "method": "POST", "path": "/api/File/UploadFile",
                        "headers": [(b"content-type", b"multipart/form-data; boundary=upload-boundary")],
                        "query_string": b"", "scheme": "http", "server": ("test", 80),
                        "client": ("test", 1), "root_path": "", "http_version": "1.1"}, receive, send)
        assert next(item for item in sent if item["type"] == "http.response.start")["status"] == 413
    asyncio.run(execute())
    assert handles and all(handle.closed for handle in handles)
    empty(fixture)


def test_dry_run_preserves_references_and_never_deletes(fixture):
    assert upload(fixture).status_code == 200
    (fixture.root / "synthetic-orphan.txt").write_text("not-referenced")
    before = {p.name: p.read_bytes() for p in fixture.root.iterdir()}
    result = asyncio.run(audit(main.settings.database_url, fixture.root, 0))
    assert result["read_only"] and result["deletions"] == 0
    assert sum(item["eligible"] for item in result["files"]) == 1
    assert next(item for item in result["files"] if item["state"] == "referenced")["eligible"] is False
    assert before == {p.name: p.read_bytes() for p in fixture.root.iterdir()}


def test_unresolved_reservation_blocks_dry_run_eligibility(fixture):
    (fixture.root / "synthetic-published-uncommitted.txt").write_text("retain")
    async def seed():
        async with SessionLocal() as session:
            session.add(UploadReservation(token="synthetic-unresolved", tenant_id=fixture.principal.tenant_id,
                                          reserved_bytes=6, file_count=1))
            await session.commit()
    asyncio.run(seed())
    result = asyncio.run(audit(main.settings.database_url, fixture.root, 0))
    assert result["reservations"] and not any(item["eligible"] for item in result["files"])


def test_limits_endpoint_preserves_unapproved_nulls_and_remote_import_disabled(fixture):
    result = fixture.client.get("/api/File/UploadLimits", headers=fixture.headers)
    assert result.status_code == 200 and all(value is None for value in result.json().values())
    assert fixture.client.post("/api/File/RemoteUploadFile", headers=fixture.headers,
                               json={"url": "http://example.invalid"}).status_code == 403

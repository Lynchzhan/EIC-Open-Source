"""部门防环与个人已读；历史数据不自动修复或回填。"""
import asyncio
from datetime import UTC, datetime
import itertools
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from fastapi.testclient import TestClient
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import select, update

from eic.core.database import SessionLocal
from auth_fixture import signed_headers
from eic.core.message_reads import read_fields
from eic.core.organization import cycle_members
from eic.main import app
from eic.models import Department, MessageReadReceipt, Resource, Tenant, User

SERIAL = itertools.count()
BACKEND = Path(__file__).parents[1]


def tag():
    return "org-message-" + str(next(SERIAL))


def login(client, username="admin", password=None):
    result = client.post("/api/Authorize/GetToken", json={
        "name": username, "passWord": password or os.environ["EIC_BOOTSTRAP_ADMIN_PASSWORD"], "tenantId": 1000,
    })
    assert result.status_code == 200, result.text
    client.cookies.clear()
    return {"Authorization": "Bearer " + result.json()}


def department(client, headers, name, parent=None):
    result = client.post("/api/Department/AddEdit", headers=headers, json={"name": name, "parentId": parent})
    assert result.status_code == 200 and result.json() is True, result.text
    rows = client.get("/api/Department/GetALLList", headers=headers).json()
    return int(next(item["id"] for item in rows if item["name"] == name))


def notice(client, headers, title, **fields):
    assert client.post("/api/Message/AddEdit", headers=headers, json={
        "title": title, "content": "synthetic", "type": "announcement", **fields,
    }).json() is True
    return next(item for item in client.post("/api/Message/GetAnnouncementPageData", headers=headers,
                                            json={"pageNum": 1, "pageSize": 500}).json()["data"] if item["title"] == title)


def get_notice(client, headers, message_id):
    rows = client.post("/api/Message/GetAnnouncementPageData", headers=headers,
                       json={"pageNum": 1, "pageSize": 500}).json()["data"]
    return next(item for item in rows if int(item["id"]) == int(message_id))


def reader(client, admin):
    name = tag()
    password = "Synthetic-Reader-Password-2026-Long"
    assert client.post("/api/User/AddEdit", headers=admin,
                       json={"username": name, "password": password, "roleIds": []}).json() is True
    return login(client, name, password)


@pytest.mark.parametrize("parents,expected", [
    ({1: None, 2: 1, 3: 2}, set()), ({1: 1}, {1}),
    ({1: 2, 2: 3, 3: 1}, {1, 2, 3}), ({1: 2, 2: 1, 3: 2, 4: None}, {1, 2}),
    ({1: 99, 2: 1}, set()),
])
def test_iterative_cycle_detection(parents, expected):
    assert cycle_members(parents) == expected


def test_deep_chain_avoids_python_recursion():
    assert cycle_members({value: value - 1 if value else None for value in range(10000)}) == set()


@pytest.mark.parametrize("legacy,expected", [(False, "unread"), (None, "unread"),
                                          (True, "unknown"), ("true", "unknown"), (1, "unknown")])
def test_unattributed_legacy_read_is_never_personal_read(legacy, expected):
    result = read_fields({"isRead": legacy})
    assert result["isRead"] is False and result["readState"] == expected


def test_personal_receipt_overrides_unattributed_legacy():
    value = datetime(2026, 10, 2, tzinfo=UTC)
    result = read_fields({"isRead": True}, value)
    assert result["isRead"] and result["readState"] == "read" and result["readAt"] == value.isoformat()


def test_ancestor_reparent_rejected_and_legal_tree_retained():
    with TestClient(app) as client:
        admin = login(client)
        name = tag()
        root = department(client, admin, name + "-root")
        child = department(client, admin, name + "-child", root)
        leaf = department(client, admin, name + "-leaf", child)
        for node, parent in [(root, leaf), (child, leaf), (leaf, leaf)]:
            rejected = client.post("/api/Department/AddEdit", headers=admin,
                                   json={"id": node, "name": name, "parentId": parent})
            assert rejected.status_code == 400
        rows = client.get("/api/Department/GetALLList", headers=admin).json()
        saved = {int(item["id"]): item for item in rows}
        assert saved[root]["parentId"] is None and int(saved[child]["parentId"]) == root
        assert client.get("/api/Department/GetDepartmentTree", headers=admin).status_code == 200
        valid = client.post("/api/Department/AddEdit", headers=admin,
                            json={"id": leaf, "name": name + "-leaf", "parentId": root})
        assert valid.status_code == 200


def test_nonexistent_and_inactive_parent_and_missing_edit_are_rejected():
    with TestClient(app) as client:
        admin = login(client)
        name = tag()
        parent = department(client, admin, name)
        assert client.delete("/api/Department/Delete", headers=admin, params={"Id": parent}).json() is True
        for parent_id in [parent, 8_999_999_999_999_999]:
            result = client.post("/api/Department/AddEdit", headers=admin, json={"name": tag(), "parentId": parent_id})
            assert result.status_code == 400
        result = client.post("/api/Department/AddEdit", headers=admin,
                             json={"id": 8_999_999_999_999_999, "name": tag()})
        assert result.status_code == 404


@pytest.mark.parametrize("route", ["delete", "disable"])
def test_active_children_block_parent_removal(route):
    with TestClient(app) as client:
        admin = login(client)
        name = tag()
        parent = department(client, admin, name)
        department(client, admin, name + "-child", parent)
        if route == "delete":
            result = client.delete("/api/Department/Delete", headers=admin, params={"Id": parent})
        else:
            result = client.post("/api/Department/AddEdit", headers=admin,
                                 json={"id": parent, "name": name, "active": False})
        assert result.status_code == 409
        assert any(int(item["id"]) == parent for item in client.get("/api/Department/GetALLList", headers=admin).json())


def test_a_read_does_not_change_b_and_is_idempotent():
    with TestClient(app) as client:
        admin = login(client)
        a, b = reader(client, admin), reader(client, admin)
        msg = notice(client, admin, tag())
        message_id = int(msg["id"])
        before_b = client.get("/api/Message/GetMyNoReadCount", headers=b).json()["count"]
        for _ in range(2):
            assert client.get("/api/Message/Read", headers=a, params={"messageId": message_id}).json() is True
        assert get_notice(client, a, message_id)["isRead"] is True
        assert get_notice(client, b, message_id)["isRead"] is False
        assert client.get("/api/Message/GetMyNoReadCount", headers=b).json()["count"] == before_b
        async def rows():
            async with SessionLocal() as session:
                receipt_rows = list(await session.scalars(select(MessageReadReceipt).where(MessageReadReceipt.message_id == message_id)))
                resource = await session.get(Resource, message_id)
                return len(receipt_rows), resource.data
        count, data = asyncio.run(rows())
        assert count == 1 and "isRead" not in data


def test_sender_cannot_forge_personal_read_and_failed_read_does_not_write():
    with TestClient(app) as client:
        admin = login(client)
        msg = notice(client, admin, tag(), isRead=True, readState="read", readAt="forged")
        assert msg["isRead"] is False and msg["readState"] == "unread"
        missing = client.get("/api/Message/Read", headers=admin, params={"messageId": 8_999_999_999_999_999})
        assert missing.json() is False


def test_deleted_message_read_does_not_create_receipt():
    with TestClient(app) as client:
        admin = login(client)
        msg = notice(client, admin, tag())
        assert client.delete("/api/Message/Delete", headers=admin, params={"Id": msg["id"]}).json() is True
        assert client.get("/api/Message/Read", headers=admin, params={"messageId": msg["id"]}).json() is False


def test_expand_preserves_legacy_resource_and_creates_no_personal_read(tmp_path):
    database = tmp_path / "old.db"
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix()}
    for revision in ["0015_runtime_schema_expand", "head", "head"]:
        result = subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", revision],
                                cwd=BACKEND, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        if revision == "0015_runtime_schema_expand":
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO tenants(id,code,name,active,created_at) VALUES(991,991,'fixture',1,CURRENT_TIMESTAMP)")
                connection.execute("""INSERT INTO resources(id,tenant_id,resource_type,name,owner_id,data,is_deleted,created_at,updated_at)
                    VALUES(992,991,'messages','legacy',NULL,'{"isRead":true}',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT data FROM resources WHERE id=992").fetchone()[0] == '{"isRead":true}'
        assert connection.execute("SELECT COUNT(*) FROM message_read_receipts").fetchone()[0] == 0
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == (
            ScriptDirectory(str(BACKEND / "migrations")).get_current_head()
        )


def test_historical_cycle_is_reported_without_silent_data_repair():
    with TestClient(app) as client:
        admin = login(client)
        name = tag()
        a = department(client, admin, name + "-a")
        b = department(client, admin, name + "-b", a)
        async def inject():
            async with SessionLocal() as session:
                await session.execute(update(Department).where(Department.id == a).values(parent_id=b))
                await session.commit()
        asyncio.run(inject())
        tree = client.get("/api/Department/GetDepartmentTree", headers=admin)
        assert tree.status_code == 409 and set(tree.json()["detail"]["departmentIds"]) == {str(a), str(b)}
        rejected = client.post("/api/Department/AddEdit", headers=admin,
                               json={"name": name + "-new", "parentId": a})
        assert rejected.status_code == 409
        rows = {int(row["id"]): row for row in client.get("/api/Department/GetALLList", headers=admin).json()}
        assert int(rows[a]["parentId"]) == b and int(rows[b]["parentId"]) == a
        # 只有显式指定的新父节点才改变历史关系。
        assert client.post("/api/Department/AddEdit", headers=admin,
                           json={"id": a, "name": name + "-a", "parentId": None}).status_code == 200


def test_legacy_read_remains_unknown_for_b_after_a_reads_and_message_edit():
    with TestClient(app) as client:
        admin = login(client)
        a, b = reader(client, admin), reader(client, admin)
        msg = notice(client, admin, tag())
        message_id = int(msg["id"])
        async def inject():
            async with SessionLocal() as session:
                item = await session.get(Resource, message_id)
                item.data = {**item.data, "isRead": True}
                await session.commit()
        asyncio.run(inject())
        before = get_notice(client, b, message_id)
        assert before["readState"] == "unknown" and before["isRead"] is False
        assert client.get("/api/Message/Read", headers=a, params={"messageId": message_id}).json() is True
        assert client.post("/api/Message/AddEdit", headers=admin, json={
            "id": message_id, "title": msg["title"], "content": "edited", "type": "announcement",
            "isRead": False,
        }).json() is True
        assert get_notice(client, a, message_id)["readState"] == "read"
        assert get_notice(client, b, message_id)["readState"] == "unknown"
        assert client.get("/api/Message/GetMyNoReadCount", headers=b).json()["unknownCount"] >= 1


def test_cross_tenant_read_edit_and_parent_reference_do_not_write():
    with TestClient(app) as client:
        admin = login(client)
        msg = notice(client, admin, tag())
        parent = department(client, admin, tag())
        async def seed():
            async with SessionLocal() as session:
                session.add(Tenant(id=700001, code=700001, name="synthetic-cross-tenant", active=True))
                await session.flush()
                session.add(User(id=700002, tenant_id=700001, username="cross-admin",
                                 password_hash="not-a-login", is_super_admin=True))
                await session.commit()
        asyncio.run(seed())
        other = signed_headers(700002, 700001, "cross-admin", "not-a-login")
        assert client.get("/api/Message/Read", headers=other, params={"messageId": msg["id"]}).json() is False
        assert client.post("/api/Message/AddEdit", headers=other,
                           json={"id": msg["id"], "title": "foreign"}).status_code == 404
        assert client.post("/api/Department/AddEdit", headers=other,
                           json={"name": "foreign-child", "parentId": parent}).status_code == 400
        assert client.post("/api/Department/AddEdit", headers=other,
                           json={"id": parent, "name": "foreign-edit"}).status_code == 404
        async def count():
            async with SessionLocal() as session:
                return len(list(await session.scalars(select(MessageReadReceipt).where(
                    MessageReadReceipt.message_id == int(msg["id"])))))
        assert asyncio.run(count()) == 0


@pytest.mark.parametrize("problem", ["missing_columns", "wrong_index"])
def test_receipt_migration_rejects_incompatible_existing_objects(tmp_path, problem):
    database = tmp_path / "incompatible.db"
    env = {**os.environ, "EIC_DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix()}
    def migrate(revision):
        return subprocess.run([sys.executable, "-B", "-m", "alembic", "upgrade", revision],
                              cwd=BACKEND, env=env, capture_output=True, text=True)
    assert migrate("0015_runtime_schema_expand").returncode == 0
    with sqlite3.connect(database) as connection:
        if problem == "missing_columns":
            connection.execute("CREATE TABLE message_read_receipts (tenant_id BIGINT PRIMARY KEY)")
        else:
            connection.execute("""CREATE TABLE message_read_receipts (
                tenant_id BIGINT NOT NULL REFERENCES tenants(id),
                message_id BIGINT NOT NULL REFERENCES resources(id),
                user_id BIGINT NOT NULL REFERENCES users(id),
                read_at DATETIME NOT NULL, PRIMARY KEY(tenant_id,message_id,user_id))""")
            connection.execute("CREATE INDEX ix_message_read_receipts_tenant_user ON message_read_receipts(message_id)")
    result = migrate("head")
    assert result.returncode != 0 and "Incompatible personal message receipt" in result.stderr
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0015_runtime_schema_expand"

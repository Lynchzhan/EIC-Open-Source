"""仅在后续获准时对 tmp_path 中的合成配置/源码归档执行；本轮未执行。"""

import importlib.util
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def deny_real_io(*args, **kwargs):
    raise AssertionError("Phase 1 release fixture forbids real network, DNS, SSH, and subprocess I/O")


@pytest.fixture(autouse=True)
def isolated_release_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.upper().startswith("EIC_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", deny_real_io)
    monkeypatch.setattr(socket, "create_connection", deny_real_io)
    monkeypatch.setattr(subprocess, "Popen", deny_real_io)
    monkeypatch.setattr(os, "system", deny_real_io)
    paramiko_stub = types.ModuleType("paramiko")
    paramiko_stub.SSHClient = deny_real_io
    paramiko_stub.RejectPolicy = deny_real_io
    monkeypatch.setitem(sys.modules, "paramiko", paramiko_stub)


def load_script(name):
    import sys

    policy = sys.modules.get("release_policy")
    if policy is not None and Path(getattr(policy, "__file__", "")).resolve() != ROOT / "scripts" / "release_policy.py":
        raise RuntimeError("release_policy was imported from a different checkout; use a fresh isolated pytest process")
    spec = importlib.util.spec_from_file_location("phase1_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
        if name == "deploy-cloud":
            module.connect = deny_real_io
            module.run = deny_real_io
            module.sftp_put = deny_real_io
    finally:
        sys.path.pop(0)
    return module


def test_release_fixture_blocks_deployment_entrypoints():
    deploy = load_script("deploy-cloud")
    assert deploy.connect is deploy.run is deploy.sftp_put is deny_real_io
    with pytest.raises(AssertionError, match="forbids real network"):
        deploy.paramiko.SSHClient()
    assert not any(name.upper().startswith("EIC_") for name in os.environ)


@pytest.mark.parametrize("relative", [
    "deploy/.env", "nested/.ENV.production", "data/main.sqlite3", "deep/eic.db-wal",
    "nested/credentials.json", "deep/bootstrap-admin-password.txt", "keys/server.pem",
    "runtime/python/private.txt", "one/two/uploads/file.txt", "one/two/secrets/key.txt",
    "migration/reports/config_key_inventory.json", "deep/redis.rdb", ".idea/workspace.xml",
])
def test_nested_secret_and_data_names_are_excluded(tmp_path, relative):
    package = load_script("package-cloud")
    assert package.should_skip(tmp_path / relative, tmp_path)


def test_archive_has_only_allowed_members_and_never_overwrites(tmp_path):
    package = load_script("package-cloud")
    source = tmp_path / "source"
    (source / "deploy").mkdir(parents=True)
    (source / "backend" / "data").mkdir(parents=True)
    (source / "deploy" / ".env").write_text("SYNTHETIC_SECRET=fixture", encoding="utf-8")
    (source / "backend" / "data" / "fixture.db").write_text("synthetic only", encoding="utf-8")
    (source / "README.md").write_text("public fixture", encoding="utf-8")
    output = tmp_path / "output.tar.gz"
    report = package.package_source(source, output)
    assert [entry["member"] for entry in report["members"]] == ["README.md"]
    assert output.with_name(output.name + ".manifest.json").is_file()
    with pytest.raises(ValueError):
        package.package_source(source, output)


def test_upgrade_preserves_effective_keys_and_admin_password():
    deploy = load_script("deploy-cloud")
    existing = (
        "EIC_JWT_SECRET=" + "j" * 64 + "\nEIC_SECRET_ENCRYPTION_KEY=" + "e" * 64
        + "\nEIC_BOOTSTRAP_ADMIN_PASSWORD=existing-fixture-password\nEIC_SEED_DEMO_DATA=true\nCUSTOM=keep\n"
    )
    once, password = deploy.prepare_environment(existing, "fixture.invalid")
    twice, second_password = deploy.prepare_environment(once, "fixture.invalid")
    assert once == twice and password is second_password is None
    assert "EIC_JWT_SECRET=" + "j" * 64 in twice
    assert "EIC_SECRET_ENCRYPTION_KEY=" + "e" * 64 in twice
    assert "existing-fixture-password" in twice and "CUSTOM=keep" in twice
    assert "EIC_SEED_DEMO_DATA=false" in twice


def test_missing_key_never_triggers_automatic_rotation():
    deploy = load_script("deploy-cloud")
    with pytest.raises(ValueError):
        deploy.prepare_environment("EIC_JWT_SECRET=" + "j" * 64, "fixture.invalid")
    with pytest.raises(ValueError):
        deploy.prepare_environment(None, "fixture.invalid")


def test_archive_manifest_validation_rejects_modified_package(tmp_path):
    package, deploy = load_script("package-cloud"), load_script("deploy-cloud")
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("public fixture", encoding="utf-8")
    output = tmp_path / "fixture.tar.gz"
    package.package_source(source, output)
    deploy.validate_source_archive(output)
    output.write_bytes(output.read_bytes() + b"changed")
    with pytest.raises(ValueError):
        deploy.validate_source_archive(output)

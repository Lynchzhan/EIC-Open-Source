import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_simulation_secrets_survive_restart_and_are_distinct(tmp_path):
    module = load("simulation-secrets")
    first = module.get_simulation_secrets(tmp_path)
    (tmp_path / "eic-sim.db").touch()
    assert module.get_simulation_secrets(tmp_path) == first
    assert first["EIC_JWT_SECRET"] != first["EIC_SECRET_ENCRYPTION_KEY"]


def test_existing_simulation_database_requires_original_keys(tmp_path):
    (tmp_path / "eic-sim.db").touch()
    with pytest.raises(ValueError, match="禁止自动替换"):
        load("simulation-secrets").get_simulation_secrets(tmp_path)
    assert not (tmp_path / "simulation.env").exists()


@pytest.mark.parametrize("content", [
    "EIC_JWT_SECRET=" + "a" * 64 + "\n",
    "EIC_JWT_SECRET=invalid\nEIC_SECRET_ENCRYPTION_KEY=invalid\n",
    "EIC_JWT_SECRET=" + "a" * 64 + "\nEIC_JWT_SECRET=" + "b" * 64 + "\n",
])
def test_simulation_rejects_corrupt_keys_without_replacement(tmp_path, content):
    path = tmp_path / "simulation.env"
    path.write_text(content)
    with pytest.raises(ValueError):
        load("simulation-secrets").get_simulation_secrets(tmp_path)
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize("mode", ["backend", "deploy"])
def test_fresh_environment_is_initialized_once(tmp_path, mode):
    folder = tmp_path / mode
    folder.mkdir()
    name = ".env.example" if mode == "backend" else "env.example"
    (folder / name).write_bytes((ROOT / mode / name).read_bytes())
    module = load("init-env")
    target = module.initialize(tmp_path, mode)
    first = target.read_text(encoding="utf-8")
    assert "EIC_BOOTSTRAP_ENABLED=true" in first
    assert "EIC_SEED_DEMO_DATA=false" in first
    assert "EIC_AUTO_CREATE_SCHEMA=false" in first
    with pytest.raises(ValueError, match="已存在"):
        module.initialize(tmp_path, mode)
    assert target.read_text(encoding="utf-8") == first


def test_existing_development_database_blocks_new_keys(tmp_path):
    folder = tmp_path / "backend"
    folder.mkdir()
    (folder / "eic.db").touch()
    with pytest.raises(ValueError, match="已有数据库"):
        load("init-env").initialize(tmp_path, "backend")


def test_source_packager_keeps_public_schema_only():
    policy = load("release_policy")
    assert not policy.excluded_source_path("backend/migrations/legacy_cloud_lite.sql")
    assert not policy.excluded_source_path("deploy/postgres-init/001-vector.sql")
    assert policy.excluded_source_path("backend/private_dump.sql")


def test_docker_has_skills_and_dev_launchers_agree():
    assert "COPY skills ./skills" in (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "8080" in (ROOT / "scripts/start-windows.ps1").read_text(encoding="utf-8")
    assert "8080" in (ROOT / "scripts/start-linux.sh").read_text(encoding="utf-8")
    assert "8080" in (ROOT / "frontend/vite.config.js").read_text(encoding="utf-8")

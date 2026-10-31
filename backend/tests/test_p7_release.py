import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[2]


def script(name):
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("p7_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", [
    "cloud_update_20260918", "cloud_update_chat_skills", "cloud_update_recalltest",
    "_cloud_fix_frontend", "_cloud_push_compose", "_cloud_diag", "_cloud_status",
    "_cloud_verify_recalltest", "_cloud_verify_chat_skills", "_cloud_verify_filter_api",
])
def test_retired_entrypoint_stops_before_network(name):
    entrypoint = ROOT / "scripts" / (name + ".py")
    result = subprocess.run([sys.executable, "-B", str(entrypoint)],
                            capture_output=True, text=True, timeout=10)
    if entrypoint.exists():
        assert result.returncode != 0 and "历史固定目标入口已停用" in result.stderr
    else:
        # 干净开源发行版已删除固定目标兼容壳；缺失入口也必须在网络操作前失败。
        assert result.returncode == 2 and "can't open file" in result.stderr


def test_deploy_script_preserves_checkout_and_requires_probe():
    module = script("release_deploy")
    manifest = {"images": {"backend": {"reference": "fixture-backend:r1", "digest": "sha256:" + "a" * 64},
                            "frontend": {"reference": "fixture-frontend:r1", "digest": "sha256:" + "b" * 64}}}
    result = module.deployment_script("/opt/p7-fixture", "p7-fixture", "docker-compose.cloud-lite.yml",
                                     "r1", "/tmp/source", "/tmp/images", "/tmp/env", "a" * 64, "b" * 64, manifest)
    assert "|| true" not in result and "schema check" in result and "api/Health/Ready" in result
    assert "--no-build --pull never" in result and "sha256sum -c -" in result
    assert "docker compose -p p7-fixture" in result and "mv /opt/" not in result
    assert "docker volume" not in result and " down" not in result


def test_backup_restore_real_files_reject_corruption_and_existing_target(tmp_path):
    import sqlite3
    module = script("recovery_snapshot")
    root = tmp_path / "synthetic"
    (root / "uploads").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "uploads/doc.txt").write_text("synthetic")
    (root / "config/.env").write_text("EIC_SECRET_ENCRYPTION_KEY=synthetic-test-only")
    with sqlite3.connect(root / "eic.db") as connection:
        connection.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO fixture VALUES(1)")
    backup = tmp_path / "snapshot"
    with pytest.raises(ValueError):
        module.backup(root, backup, tmp_path)
    result = module.backup(root, backup, tmp_path, quiesced=True)
    restored = tmp_path / "restored"
    assert module.restore(backup, restored, tmp_path)["files"] == 3
    with sqlite3.connect(restored / "eic.db") as connection:
        assert connection.execute("SELECT * FROM fixture").fetchall() == [(1,)]
    with pytest.raises(ValueError):
        module.restore(backup, restored, tmp_path)
    (backup / "uploads/doc.txt").write_text("corruption")
    with pytest.raises(ValueError):
        module.restore(backup, tmp_path / "bad-restore", tmp_path)
    assert not (tmp_path / "bad-restore").exists() and result["quiesced"]


def test_snapshot_rejects_database_symlink_before_reading(tmp_path):
    import sqlite3
    module = script("recovery_snapshot")
    root = tmp_path / "fixture"
    (root / "uploads").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config/.env").write_text("synthetic-only")
    outside = tmp_path / "outside.db"
    with sqlite3.connect(outside) as db:
        db.execute("CREATE TABLE fixture(id INTEGER)")
    (root / "eic.db").symlink_to(outside)
    with pytest.raises(ValueError, match="含链接"):
        module.backup(root, tmp_path / "bad", tmp_path, quiesced=True)
    assert not (tmp_path / "bad").exists()


def test_image_archive_requires_real_config_digest(tmp_path):
    import hashlib
    import io
    import json
    import tarfile
    module = script("release_deploy")
    config = b'{"architecture":"amd64","os":"linux","config":{}}'
    path = tmp_path / "images.tar"
    with tarfile.open(path, "w") as archive:
        for name, data in (("config.json", config),
                           ("manifest.json", json.dumps([{"Config": "config.json"}]).encode())):
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    module.validate_image_archive(path, {"backend": {"digest": "sha256:" + hashlib.sha256(config).hexdigest()}})
    with pytest.raises(ValueError, match="image ID"):
        module.validate_image_archive(path, {"backend": {"digest": "sha256:" + "0" * 64}})


def test_snapshot_rejects_linked_component_directory(tmp_path):
    import sqlite3
    module = script("recovery_snapshot")
    root = tmp_path / "fixture"
    (root / "uploads").mkdir(parents=True)
    (root / "real-config").mkdir()
    (root / "real-config/.env").write_text("synthetic-only")
    (root / "config").symlink_to(root / "real-config", target_is_directory=True)
    with sqlite3.connect(root / "eic.db") as db:
        db.execute("CREATE TABLE fixture(id INTEGER)")
    with pytest.raises(ValueError):
        module.backup(root, tmp_path / "bad", tmp_path, quiesced=True)
    assert not (tmp_path / "bad").exists()


def test_simulation_launcher_preserves_keys_and_requires_explicit_install():
    text = (ROOT / "scripts/start-sim.ps1").read_text(encoding="utf-8")
    assert "Get-SimulationSecrets" in text and "$env:EIC_SECRET_ENCRYPTION_KEY = $jwt" not in text
    assert "[switch]$InstallDependencies" in text and "if ($InstallDependencies)" in text
    helper = (ROOT / "scripts/simulation-secrets.ps1").read_text(encoding="utf-8")
    assert "FileMode]::CreateNew" in helper and "禁止自动替换" in helper


def test_restore_preserves_empty_upload_component(tmp_path):
    import sqlite3
    module = script("recovery_snapshot")
    root = tmp_path / "fixture"
    (root / "uploads").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config/.env").write_text("synthetic-only")
    with sqlite3.connect(root / "eic.db") as db:
        db.execute("CREATE TABLE fixture(id INTEGER)")
    module.backup(root, tmp_path / "snapshot", tmp_path, quiesced=True)
    module.restore(tmp_path / "snapshot", tmp_path / "restored", tmp_path)
    assert (tmp_path / "restored/uploads").is_dir()

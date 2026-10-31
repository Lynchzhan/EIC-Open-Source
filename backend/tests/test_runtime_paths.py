from pathlib import Path

from eic.core.config import BACKEND_ROOT, Settings


def test_relative_runtime_paths_are_based_at_backend_root(tmp_path: Path) -> None:
    settings = Settings(skill_root="skills", upload_root="uploads")

    assert settings.runtime_path(settings.skill_root) == BACKEND_ROOT / "skills"
    assert settings.runtime_path(settings.upload_root) == BACKEND_ROOT / "uploads"


def test_absolute_runtime_paths_are_preserved(tmp_path: Path) -> None:
    settings = Settings(skill_root=str(tmp_path / "skills"), upload_root=str(tmp_path / "uploads"))

    assert settings.runtime_path(settings.skill_root) == tmp_path / "skills"
    assert settings.runtime_path(settings.upload_root) == tmp_path / "uploads"

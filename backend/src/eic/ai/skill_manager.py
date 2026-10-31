from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eic.ai.declarative_skills import parse_skill_document, parse_skill_package


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    version: str
    enabled: bool
    allowed_tools: tuple[str, ...]
    permissions: tuple[str, ...]
    path: str
    content_hash: str


@dataclass(frozen=True)
class SkillLoadFailure:
    path: str
    error: str


class SkillManager:
    """Loads declarative SKILL.md files without importing or executing their scripts."""

    def discover(self, root: Path) -> tuple[list[LoadedSkill], list[SkillLoadFailure]]:
        if not root.exists():
            return [], []
        skills: list[LoadedSkill] = []
        failures: list[SkillLoadFailure] = []
        names: set[str] = set()
        for manifest in sorted(root.glob("*/SKILL.md")):
            try:
                if manifest.is_symlink() or manifest.parent.is_symlink() or not manifest.resolve().is_relative_to(root.resolve()):
                    raise ValueError("技能声明不能跨越发现根目录")
                skill = self._parse(manifest)
                if skill.name in names:
                    raise ValueError(f"duplicate skill name: {skill.name}")
                names.add(skill.name)
                skills.append(skill)
            except (OSError, TypeError, ValueError) as exc:
                failures.append(SkillLoadFailure(str(manifest), str(exc)))
        return skills, failures

    def cache_snapshot(self, root: Path) -> dict[str, Any]:
        skills, failures = self.discover(root)
        return {"skills": [asdict(skill) for skill in skills], "failures": [asdict(item) for item in failures]}

    @staticmethod
    def _parse(manifest: Path) -> LoadedSkill:
        if manifest.stat().st_size > 65536:
            raise ValueError("技能声明文件过大")
        raw = manifest.read_text(encoding="utf-8")
        data = parse_skill_document(raw).data
        return LoadedSkill(
            name=data["name"],
            description=str(data.get("description", "")).strip(),
            version=str(data.get("version", "0.0.0")),
            enabled=bool(data.get("enabled", True)),
            allowed_tools=tuple(data["allowed_tools"]),
            permissions=tuple(data["permissions"]),
            path=str(manifest),
            content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )


def validate_skill_archive(path: Path) -> None:
    """与真实执行使用相同的严格声明式包解析，不解压或执行上传内容。"""
    with path.open("rb") as stream:
        payload = stream.read(131073)
    parse_skill_package(payload)

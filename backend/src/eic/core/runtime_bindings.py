"""应用装配提供的运行资源契约，领域层无需依赖 API 层。"""
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeBindings:
    settings_provider: Callable[[], Any]
    sessions_provider: Callable[[], Any]
    engine_provider: Callable[[], Any]
    tools_provider: Callable[[], Any]
    skills_provider: Callable[[], Any]
    upload_root_provider: Callable[[], Path]
    skill_root_provider: Callable[[], Path]
    graph_provider: Callable[[], Any]

    @property
    def settings(self):
        return self.settings_provider()

    @property
    def sessions(self):
        return self.sessions_provider()

    @property
    def engine(self):
        return self.engine_provider()

    @property
    def tools(self):
        return self.tools_provider()

    @property
    def skills(self):
        return self.skills_provider()

    @property
    def upload_root(self):
        return self.upload_root_provider()

    @property
    def skill_root(self):
        return self.skill_root_provider()

    @property
    def graph(self):
        return self.graph_provider()

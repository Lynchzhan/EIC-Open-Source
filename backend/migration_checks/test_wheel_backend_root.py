"""发布资源定位必须兼容源码布局及实际wheel安装布局。"""
import importlib.util
from pathlib import Path
import shutil
import sys

import pytest


@pytest.mark.parametrize('layout', ['src/eic/core', '.venv/lib/python3.11/site-packages/eic/core'])
def test_backend_assets_resolve_from_source_and_installed_wheel(tmp_path, monkeypatch, layout):
    from eic.core import config
    backend = tmp_path / 'application'
    module_dir = backend / layout
    module_dir.mkdir(parents=True)
    (backend / 'migrations').mkdir()
    (backend / 'migrations/env.py').write_text('', encoding='utf-8')
    (backend / 'alembic.ini').write_text('[alembic]\n', encoding='utf-8')
    (backend / 'pyproject.toml').write_text('[project]\nname="synthetic"\n', encoding='utf-8')
    # cwd中相同标记不能改变安装模块所属部署根。
    decoy = tmp_path / 'unrelated'
    decoy.mkdir()
    (decoy / 'migrations').mkdir()
    (decoy / 'alembic.ini').write_text('[alembic]\n', encoding='utf-8')
    (decoy / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')
    monkeypatch.chdir(decoy)
    target = module_dir / 'config.py'
    shutil.copyfile(config.__file__, target)
    name = '_eic_wheel_root_regression'
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    assert module.BACKEND_ROOT == backend
    assert Path(module.Settings.model_fields['skill_root'].default) == backend / 'skills'
    assert Path(module.Settings.model_fields['upload_root'].default) == backend / 'uploads'
    assert (module.BACKEND_ROOT / 'migrations/env.py').is_file()

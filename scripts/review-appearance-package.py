"""只解析源码与构建产物，不启动应用、不连接数据库或模型。"""
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

root = Path(__file__).resolve().parents[1]
sources = sorted((root / "backend/src/eic").rglob("*.py"))
for source in sources:
    ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
wheel = root / "backend/dist/enterprise_intelligence_center-0.1.0-py3-none-any.whl"
with ZipFile(wheel) as archive:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise RuntimeError("wheel 中出现重复条目")
    for name in ("eic/appearance_preferences.py", "eic/main.py", "eic/codegen_templates/README.md.tpl"):
        if archive.read(name) != (root / "backend/src" / name).read_bytes():
            raise RuntimeError(f"wheel 源码不一致：{name}")
    for name in ("eic/skills/text-statistics/SKILL.md", "eic/skills/json-format/SKILL.md", "eic/skills/csv-summary/SKILL.md"):
        if name not in names:
            raise RuntimeError(f"wheel 缺少技能资源：{name}")
artifacts = [wheel, *sorted((root / "frontend/dist").rglob("*"))]
print(json.dumps({"mode": "static-source-and-package-review", "pythonFiles": len(sources),
    "artifacts": [{"path": str(p.relative_to(root)), "bytes": p.stat().st_size,
                   "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in artifacts if p.is_file()]}, ensure_ascii=False, indent=2))
